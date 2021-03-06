import bisect
import datetime
import hashlib
import itertools
import logging
import math
import operator as op
import os.path
import random

from collections import abc, OrderedDict, Iterable, Mapping, Counter, namedtuple
from collections.abc import Sequence
from functools import wraps, partial
from importlib import import_module
from inspect import signature
from math import log10, floor, ceil
from time import sleep
from urllib.parse import urlparse

from toolz.dicttoolz import *
from toolz.functoolz import identity

# Configure logging

logging.basicConfig(format='[%(asctime)s] %(name)s:%(levelname)s - %(message)s', datefmt='%H:%M:%S', level=logging.INFO)
logging.getLogger("requests").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)

# Classes

class MissingModule(object):
    """A class representing a missing module import: see optional_import."""
    def __init__(self, module, bindings):
        self._module = module
        for k,v in bindings.items():
            setattr(self, k, v)
    def __getattr__(self, k):
        raise ImportError("Missing module: {}".format(self._module))
    def __bool__(self):
        return False
    def __repr__(self):
        return "<MissingModule: {}>".format(self._module)
        
def optional_import(module, **bindings):
    """Optionally load the named module, returning a MissingModule
    object on failure, optionally with the given bindings."""
    try:
        return import_module(module)
    except ImportError:
        return MissingModule(module, bindings)
   
def optional_import_from(module, identifier, default=None):
    """Optionally import an identifier from the named module, returning the
    default value on failure."""
    return optional_import(module).__dict__.get(identifier, default)
    
class ValueCache():
    """A simple container with a returning assignment operator."""
    def __init__(self, value=None):
        self.value = value
    def __pos__(self):
        return self.value
    def __repr__(self):
        return "ValueCache({})".format(self.value)
    def set(self, value):
        self.value = value
        return value

# Decorators

def number_of_args(fn):
    """Return the number of positional arguments for a function, or None if the number is variable.
    Looks inside any decorated functions."""
    try:
        if hasattr(fn, '__wrapped__'):
            return number_of_args(fn.__wrapped__)
        if any(p.kind == p.VAR_POSITIONAL for p in signature(fn).parameters.values()):
            return None
        else:
            return sum(p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD) for p in signature(fn).parameters.values())
    except ValueError:
        # signatures don't work for built-in operators, so check for a few explicitly
        UNARY_OPS = [len, op.not_, op.truth, op.abs, op.index, op.inv, op.invert, op.neg, op.pos]
        BINARY_OPS = [op.lt, op.le, op.gt, op.ge, op.eq, op.ne, op.is_, op.is_not, op.add, op.and_, op.floordiv, op.lshift, op.mod, op.mul, op.or_, op.pow, op.rshift, op.sub, op.truediv, op.xor, op.concat, op.contains, op.countOf, op.delitem, op.getitem, op.indexOf]
        TERNARY_OPS = [op.setitem]
        if fn in UNARY_OPS:
            return 1
        elif fn in BINARY_OPS:
            return 2
        elif fn in TERNARY_OPS:
            return 3
        else:
            raise NotImplementedError("Bult-in operator {} not supported".format(fn))
      
def all_keyword_args(fn):
    """Return the names of all the keyword arguments for a function, or None if the number is variable.
    Looks inside any decorated functions."""
    try:
        if hasattr(fn, '__wrapped__'):
            return all_keyword_args(fn.__wrapped__)
        elif any(p.kind == p.VAR_KEYWORD for  p in signature(fn).parameters.values()):
            return None
        else:
            return [p.name for p in signature(fn).parameters.values() if p.kind in (p.KEYWORD_ONLY, p.POSITIONAL_OR_KEYWORD)]
    except ValueError:
        # signatures don't work for built-in operators, so check for a few explicitly, otherwise assume none
        BUILTINS = { }
        return BUILTINS.get(fn, [])
        
def ignoring_extra_args(fn):
    """Function decorator that calls the wrapped function with
    correct number of positional arguments, discarding any
    additional arguments."""
    n = number_of_args(fn)
    kwa = all_keyword_args(fn)
    @wraps(fn)
    def wrapper(*args, **kwargs):
        return fn(*args[0:n], **keyfilter(lambda k: kwa is None or k in kwa, kwargs))
    return wrapper

def ignoring_exceptions(fn, handler=None, exceptions=Exception):
    """Function decorator that catches exceptions, returning instead."""
    handler_fn = ignoring_extra_args(handler if callable(handler) else lambda: handler)
    @wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except exceptions:
            return handler_fn(*args, **kwargs)
    return wrapper

def with_retries(fn, max_retries=None, max_duration=None, interval=0.5, exceptions=Exception):
    """Function decorator that retries the function when exceptions are raised."""
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if max_duration is None:
            end_time = datetime.datetime.max
        else:
            end_time = datetime.datetime.now() + datetime.timedelta(seconds=max_duration)
        for i in itertools.count() if max_retries is None else range(max_retries):
            try:
                return fn(*args, **kwargs)
            except exceptions:
                if i + 1 == max_retries: raise
                elif datetime.datetime.now() > datetime.datetime.max: raise
                else: sleep(interval)
    return wrapper

class cached_property(object):
    """Cached property decorator. Cache expires after a set interval or on deletion."""

    def __init__(self, fn, expires_after=None):
        self.__doc__ = fn.__doc__
        self.fn = fn
        self.name = fn.__name__
        self.expires_after = expires_after

    def __get__(self, obj, owner=None):
        if obj is None:
            return self
        if not hasattr(obj, '_property_cache_expiry_times'):
            obj._property_cache_expiry_times = {}
        if not hasattr(obj, '_property_cache_values'):
            obj._property_cache_values = {}
        if (obj._property_cache_expiry_times.get(self.name) is None or
            datetime.datetime.now() > obj._property_cache_expiry_times[self.name]):
            obj._property_cache_values[self.name] = self.fn(obj)
            if self.expires_after is None:
                obj._property_cache_expiry_times[self.name] = datetime.datetime.max
            else:
                obj._property_cache_expiry_times[self.name] = datetime.datetime.now() + datetime.timedelta(seconds=self.expires_after)
        return obj._property_cache_values[self.name]
            
    def __delete__(self, obj):
        if self.name in getattr(obj, '_property_cache_expiry_times', {}):
            del obj._property_cache_expiry_times[self.name]
        if self.name in getattr(obj, '_property_cache_values', {}):
            del obj._property_cache_values[self.name]
            
def cached_property_expires_after(expires_after):
    return partial(cached_property, expires_after=expires_after)

# Iterables
        
def non_string_iterable(v):
    """Return whether the object is any Iterable other than str."""
    return isinstance(v, Iterable) and not isinstance(v, str)

def make_iterable(v):
    """Return an iterable from an object, wrapping it in a tuple if needed."""
    return v if non_string_iterable(v) else () if v is None else (v,)
    
def non_string_sequence(v, types=None):
    """Return whether the object is a Sequence other than str, optionally 
    with the given element types."""
    return isinstance(v, Sequence) and (types is None or all(any(isinstance(x, t) for t in make_iterable(types)) for x in v))
    
def make_sequence(v):
    """Return a sequence from an object, wrapping it in a tuple if needed."""
    return v if non_string_sequence(v) else () if v is None else (v,)
    
def remove_duplicates(seq, key=lambda v:v, keep_last=False):
    """Return an order preserving tuple copy containing items from an iterable, deduplicated
    based on the given key function."""
    d = OrderedDict()
    for x in seq:
        k = key(x)
        if keep_last and k in d:
            del d[k]
        if keep_last or k not in d:
            d[k] = x
    return tuple(d.values())

def first_or_default(iterable, default=None):
    """Return the first element of an iterable, or None if there aren't any."""
    try:
        return next(x for x in iter(iterable))
    except StopIteration:
        return default
    
def is_in(x, l):
    """Whether x is the same object as any member of l"""
    return any(x is y for y in l)

def update_sequence(s, n, x):
    """Return a tuple copy of s with the nth element replaced by x."""
    t = tuple(s)
    if -len(t) <= n < len(t):
        return t[0:n] + (x,) + t[n+1:0 if n ==-1 else None]
    else:
        raise IndexError("sequence index out of range")

# Generators

def generate_leafs(iterable):
    """Generator that yields all the leaf nodes of an iterable."""
    for x in iterable:
        if non_string_iterable(x):
            yield from leafs(x)
        else:
            yield x
            
def generate_batches(iterable, batch_size):
    """Generator that yields the elements of an iterable n at a time."""
    sourceiter = iter(iterable)
    while True:
        slice = list(itertools.islice(sourceiter, batch_size))
        if len(slice) == 0: break
        yield slice

def generate_ngrams(iterable, n):
    """Generator that yields n-grams from a iterable."""
    return zip(*[itertools.islice(it,i,None) for i,it in enumerate(itertools.tee(iterable, n))])

def repeat_each(iterable, repeats):
    """Generator that yields the elements of an iterable, repeated n times each."""
    return (p[0] for p in itertools.product(iterable, range(repeats)))

def filter_proportion(iterable, proportion):
    """Generator that yields 0 < proportion <= 1 of the elements of an iterable."""
    if not 0 < proportion <= 1:
        raise ValueError("Filter proportion must be between 0 and 1")
    sourceiter, p = iter(iterable), 0
    while True:
        v = next(sourceiter)
        p += proportion
        if p >= 1:
            p -= 1
            yield v

def generate_subsequences(iterable, start_if, end_if):
    """Generator that returns subsequences based on start and end condition functions. Both functions get passed the current element, while the end function optionally gets passed the current subsequence too."""
    sourceiter = iter(iterable)
    while True:
        start = next(x for x in sourceiter if start_if(x))
        x, subseq = start, [start]
        while not ignoring_extra_args(end_if)(x, subseq):
            x = next(sourceiter)
            subseq.append(x)
        yield subseq

def riffle_shuffle(iterable, n=2):
    """Generator that performs a perfect riffle shuffle on the input, using a given number of subdecks."""
    return itertools.filterfalse(none_or_nan, itertools.chain.from_iterable(zip(*list(itertools.zip_longest(*[iter(iterable)]*n)))))

# Mappings
    
def none_or_nan(x):
    """Whether the object is None or a float nan."""
    return x is None or isinstance(x, float) and math.isnan(x)
    
def get_non(d, k, default=None):
    """Like get but treats None and nan as missing values."""
    v = d.get(k, default)
    return default if none_or_nan(v) else v
    
def make_mapping(v, key_fn=identity):
    """Return a mapping from an object, using a function to generate keys if needed.
    Mappings are left as is, iterables are split into elements, everything else is
    wrapped in a singleton map."""
    if isinstance(v, Mapping): return v
    elif non_string_iterable(v): return { ignoring_extra_args(key_fn)(i, x) : x for (i,x) in enumerate(v) }
    else: return { ignoring_extra_args(key_fn)(None, v) : v }

def merge_dicts(*dicts, merge_fn=lambda k, *vs: vs[-1]):
    """Merge a collection of dicts using the merge function, which is
    a function on conflicting field names and values."""
    def item_map(kv): return (kv[0], kv[1][0] if len(kv[1]) == 1 else merge_fn(kv[0], *kv[1]))
    return itemmap(item_map, merge_with(list, *dicts))

# Functions

def papply(func, *args, **kwargs):
    """Like functoools.partial, but also postpones evaluation of any positional arguments
    with a value of Ellipsis (...): e.g. papply(print, ..., 2, ..., 4)(1, 3, 5) prints 1 2 3 4 5."""
    min_args = sum(int(x is Ellipsis) for x in args)
    def newfunc(*fargs, **fkwargs):
        if len(fargs) < min_args:
            raise TypeError("Partial application expects at least {} positional arguments but {} were given".format(min_args, len(fargs)))
        newkwargs = kwargs.copy()
        newkwargs.update(fkwargs)
        newargs, i = [], 0
        for arg in args:
            if arg is Ellipsis:
                newargs.append(fargs[i])
                i += 1
            else:
                newargs.append(arg)
        newargs += fargs[i:]
        return func(*newargs, **newkwargs)
    return newfunc
    
def artial(func, *args, **kwargs):
    """Like functools.partial, but always omits the first positional argument."""
    def newfunc(*fargs, **fkwargs):
        if len(fargs) == 0:
            raise TypeError("Partial application expects at least 1 positional arguments but 0 were given")
        newkwargs = kwargs.copy()
        newkwargs.update(fkwargs)
        rargs = args + fargs[1:]
        return func(fargs[0], *rargs, **newkwargs)
    return newfunc
    
# Data structures

class CaseInsensitiveDict(abc.MutableMapping):
    """Case-insensitive dict."""
    
    def __init__(self, d={}, normalize=str.lower, base_factory=dict):
        self.normalize = normalize
        self._d = base_factory()
        self._k = {}
        if isinstance(d, abc.Mapping):
            for k, v in d.items():
                self.__setitem__(k, v)
        elif isinstance(d, abc.Iterable):
            for (k, v) in d:
                self.__setitem__(k, v)
    
    def __getitem__(self, k):
        was_missing = self.normalize(k) not in self._d
        v = self._d[self.normalize(k)]
        if was_missing and k.lower() in self._d:
            # must be using a defaultdict of some kind
            self._k[self.normalize(k)] = k
        return v
    
    def __setitem__(self, k, v):
        self._d[self.normalize(k)] = v
        self._k[self.normalize(k)] = k
        
    def __delitem__(self, k):
        del self._d[self.normalize(k)]
        del self._k[self.normalize(k)]

    def __iter__(self):
        return (self._k[k] for k in self._d)
        
    def __len__(self):
        return len(self._d)
        
    def __repr__(self):
        return "{" + ", ".join("%r: %r" % (self._k[k], v) for (k, v) in self._d.items()) + "}"
        
    def copy(self):
        return CaseInsensitiveDict(self)
        
# Numeric

def sign(x):
    """Sign indication of a number"""
    return 1 if x > 0 else -1 if x < 0 else 0
    
def round_significant(x, n=1):
    """Round x to n significant digits."""
    return 0 if x==0 else round(x, -int(floor(log10(abs(x)))) + (n-1))
    
def floor_digits(x, n=0):
    """Floor x to n decimal digits."""
    return floor(x * 10**n) / 10**n
    
def floor_significant(x, n=1):
    """Floor x to n significant digits."""
    return 0 if x==0 else floor_digits(x, -int(floor(log10(abs(x)))) + (n-1))

def ceil_digits(x, n=0):
    """Ceil x to n decimal digits."""
    return ceil(x * 10**n) / 10**n
    
def ceil_significant(x, n=1):
    """Ceil x to n significant digits."""
    return 0 if x==0 else ceil_digits(x, -int(floor(log10(abs(x)))) + (n-1))
    
def delimit(x, low, high):
    """Delimit x so that it lies between the low and high marks."""
    return max(low, min(x, high))

def weighted_choices(seq, weights, n):
    """Return random elements from a sequence, according to the given relative weights."""
    cum = list(itertools.accumulate(weights, op.add))
    return [seq[bisect.bisect_left(cum, random.uniform(0, cum[-1]))] for i in range(n)]

def weighted_choice(seq, weights):
    """Return a single random elements from a sequence, according to the given relative weights."""
    return weighted_choices(seq, weights, n=1)[0]

def _Counter_randoms(self, n, filter=None):
    """Return random elements from the Counter collection, weighted by count."""
    d = self if filter is None else { k : v for k,v in self.items() if filter(k) }
    return weighted_choices(list(d.keys()), list(d.values()), n=n)
    
def _Counter_random(self, filter=None):
    """Return a single random elements from the Counter collection, weighted by count."""
    return _Counter_randoms(self, 1, filter=filter)[0]
    
Counter.random_choices = _Counter_randoms
Counter.random_choice = _Counter_random

# Network/io

def printed(o, **kwargs):
    """Print an object and return it"""
    return print(o, **kwargs) or o

def url_to_filepath(url):
    """Convert url to a filepath of the form hostname/hash-of-path.extension. Ignores protocol, port, query and fragment."""
    uparse = urlparse(url)
    upath, uext = os.path.splitext(uparse.path)
    uname = hashlib.sha1(upath.encode('utf-8')).hexdigest()
    return os.path.join(uparse.netloc, uname + uext)
   
