"""Pickle related utilities. Perhaps this should be called 'can'."""

# Copyright (c) IPython Development Team.
# Distributed under the terms of the Modified BSD License.

import warnings
warnings.warn("ipykernel.pickleutil is deprecated. It has moved to ipyparallel.",
    DeprecationWarning,
    stacklevel=2
)

import copy
import sys
import pickle
from types import FunctionType

from traitlets.utils.importstring import import_item
from ipython_genutils.py3compat import buffer_to_bytes

# This registers a hook when it's imported
try:
    # available since ipyparallel 5.1.1
    from ipyparallel.serialize import codeutil
except ImportError:
    # Deprecated since ipykernel 4.3.1
    from ipykernel import codeutil

from traitlets.log import get_logger

buffer = memoryview
class_type = type

PICKLE_PROTOCOL = pickle.DEFAULT_PROTOCOL

def _get_cell_type(a=None):
    """the type of a closure cell doesn't seem to be importable,
    so just create one
    """
    def inner():
        return a
    return type(inner.__closure__[0])

cell_type = _get_cell_type()

#-------------------------------------------------------------------------------
# Functions
#-------------------------------------------------------------------------------


def interactive(f):
    """decorator for making functions appear as interactively defined.
    This results in the function being linked to the user_ns as globals()
    instead of the module globals().
    """
    
    # build new FunctionType, so it can have the right globals
    # interactive functions never have closures, that's kind of the point
    if isinstance(f, FunctionType):
        mainmod = __import__('__main__')
        f = FunctionType(f.__code__, mainmod.__dict__,
            f.__name__, f.__defaults__,
        )
    # associate with __main__ for uncanning
    f.__module__ = '__main__'
    return f


def use_dill():
    """use dill to expand serialization support

    adds support for object methods and closures to serialization.
    """
    # import dill causes most of the magic
    import dill
    
    # dill doesn't work with cPickle,
    # tell the two relevant modules to use plain pickle
    
    global pickle
    pickle = dill

    try:
        from ipykernel import serialize
    except ImportError:
        pass
    else:
        serialize.pickle = dill
    
    # disable special function handling, let dill take care of it
    can_map.pop(FunctionType, None)

def use_cloudpickle():
    """use cloudpickle to expand serialization support

    adds support for object methods and closures to serialization.
    """
    import cloudpickle
    
    global pickle
    pickle = cloudpickle

    try:
        from ipykernel import serialize
    except ImportError:
        pass
    else:
        serialize.pickle = cloudpickle
    
    # disable special function handling, let cloudpickle take care of it
    can_map.pop(FunctionType, None)


#-------------------------------------------------------------------------------
# Classes
#-------------------------------------------------------------------------------


class CannedObject(object):
    def __init__(self, obj, keys=[], hook=None):
        """can an object for safe pickling

        Parameters
        ----------
        obj
            The object to be canned
        keys : list (optional)
            list of attribute names that will be explicitly canned / uncanned
        hook : callable (optional)
            An optional extra callable,
            which can do additional processing of the uncanned object.

        Notes
        -----
        large data may be offloaded into the buffers list,
        used for zero-copy transfers.
        """
        self.keys = keys
        self.obj = copy.copy(obj)
        self.hook = can(hook)
        for key in keys:
            setattr(self.obj, key, can(getattr(obj, key)))
        
        self.buffers = []

    def get_object(self, g=None):
        if g is None:
            g = {}
        obj = self.obj
        for key in self.keys:
            setattr(obj, key, uncan(getattr(obj, key), g))
        
        if self.hook:
            self.hook = uncan(self.hook, g)
            self.hook(obj, g)
        return self.obj
    

class Reference(CannedObject):
    """object for wrapping a remote reference by name."""
    def __init__(self, name):
        if not isinstance(name, str):
            raise TypeError("illegal name: %r"%name)
        self.name = name
        self.buffers = []

    def __repr__(self):
        return "<Reference: %r>"%self.name

    def get_object(self, g=None):
        if g is None:
            g = {}
        
        return eval(self.name, g)


class CannedCell(CannedObject):
    """Can a closure cell"""
    def __init__(self, cell):
        self.cell_contents = can(cell.cell_contents)
    
    def get_object(self, g=None):
        cell_contents = uncan(self.cell_contents, g)
        def inner():
            return cell_contents
        return inner.__closure__[0]


class CannedFunction(CannedObject):

    def __init__(self, f):
        self._check_type(f)
        self.code = f.__code__
        if f.__defaults__:
            self.defaults = [ can(fd) for fd in f.__defaults__ ]
        else:
            self.defaults = None
        
        closure = f.__closure__
        if closure:
            self.closure = tuple( can(cell) for cell in closure )
        else:
            self.closure = None
        
        self.module = f.__module__ or '__main__'
        self.__name__ = f.__name__
        self.buffers = []

    def _check_type(self, obj):
        assert isinstance(obj, FunctionType), "Not a function type"

    def get_object(self, g=None):
        # try to load function back into its module:
        if not self.module.startswith('__'):
            __import__(self.module)
            g = sys.modules[self.module].__dict__

        if g is None:
            g = {}
        if self.defaults:
            defaults = tuple(uncan(cfd, g) for cfd in self.defaults)
        else:
            defaults = None
        if self.closure:
            closure = tuple(uncan(cell, g) for cell in self.closure)
        else:
            closure = None
        newFunc = FunctionType(self.code, g, self.__name__, defaults, closure)
        return newFunc

class CannedClass(CannedObject):

    def __init__(self, cls):
        self._check_type(cls)
        self.name = cls.__name__
        self.old_style = not isinstance(cls, type)
        self._canned_dict = {}
        for k,v in cls.__dict__.items():
            if k not in ('__weakref__', '__dict__'):
                self._canned_dict[k] = can(v)
        if self.old_style:
            mro = []
        else:
            mro = cls.mro()
        
        self.parents = [ can(c) for c in mro[1:] ]
        self.buffers = []

    def _check_type(self, obj):
        assert isinstance(obj, class_type), "Not a class type"

    def get_object(self, g=None):
        parents = tuple(uncan(p, g) for p in self.parents)
        return type(self.name, parents, uncan_dict(self._canned_dict, g=g))

class CannedArray(CannedObject):
    def __init__(self, obj):
        from numpy import ascontiguousarray
        self.shape = obj.shape
        self.dtype = obj.dtype.descr if obj.dtype.fields else obj.dtype.str
        self.pickled = False
        if sum(obj.shape) == 0:
            self.pickled = True
        elif obj.dtype == 'O':
            # can't handle object dtype with buffer approach
            self.pickled = True
        elif obj.dtype.fields and any(dt == 'O' for dt,sz in obj.dtype.fields.values()):
            self.pickled = True
        if self.pickled:
            # just pickle it
            self.buffers = [pickle.dumps(obj, PICKLE_PROTOCOL)]
        else:
            # ensure contiguous
            obj = ascontiguousarray(obj, dtype=None)
            self.buffers = [buffer(obj)]
    
    def get_object(self, g=None):
        from numpy import frombuffer
        data = self.buffers[0]
        if self.pickled:
            # we just pickled it
            return pickle.loads(data)
        else:
            return frombuffer(data, dtype=self.dtype).reshape(self.shape)


class CannedBytes(CannedObject):
    wrap = staticmethod(buffer_to_bytes)

    def __init__(self, obj):
        self.buffers = [obj]
    
    def get_object(self, g=None):
        data = self.buffers[0]
        return self.wrap(data)

class CannedBuffer(CannedBytes):
    wrap = buffer

class CannedMemoryView(CannedBytes):
    wrap = memoryview

#-------------------------------------------------------------------------------
# Functions
#-------------------------------------------------------------------------------

def _import_mapping(mapping, original=None):
    """import any string-keys in a type mapping

    """
    log = get_logger()
    log.debug("Importing canning map")
    for key,value in list(mapping.items()):
        if isinstance(key, str):
            try:
                cls = import_item(key)
            except Exception:
                if original and key not in original:
                    # only message on user-added classes
                    log.error("canning class not importable: %r", key, exc_info=True)
                mapping.pop(key)
            else:
                mapping[cls] = mapping.pop(key)

def istype(obj, check):
    """like isinstance(obj, check), but strict

    This won't catch subclasses.
    """
    if isinstance(check, tuple):
        for cls in check:
            if type(obj) is cls:
                return True
        return False
    else:
        return type(obj) is check

def can(obj):
    """prepare an object for pickling"""
    
    import_needed = False
    
    for cls,canner in can_map.items():
        if isinstance(cls, str):
            import_needed = True
            break
        elif istype(obj, cls):
            return canner(obj)
    
    if import_needed:
        # perform can_map imports, then try again
        # this will usually only happen once
        _import_mapping(can_map, _original_can_map)
        return can(obj)
    
    return obj

def can_class(obj):
    if isinstance(obj, class_type) and obj.__module__ == '__main__':
        return CannedClass(obj)
    else:
        return obj

def can_dict(obj):
    """can the *values* of a dict"""
    if istype(obj, dict):
        newobj = {}
        for k, v in obj.items():
            newobj[k] = can(v)
        return newobj
    else:
        return obj

sequence_types = (list, tuple, set)

def can_sequence(obj):
    """can the elements of a sequence"""
    if istype(obj, sequence_types):
        t = type(obj)
        return t([can(i) for i in obj])
    else:
        return obj

def uncan(obj, g=None):
    """invert canning"""
    
    import_needed = False
    for cls,uncanner in uncan_map.items():
        if isinstance(cls, str):
            import_needed = True
            break
        elif isinstance(obj, cls):
            return uncanner(obj, g)
    
    if import_needed:
        # perform uncan_map imports, then try again
        # this will usually only happen once
        _import_mapping(uncan_map, _original_uncan_map)
        return uncan(obj, g)
    
    return obj

def uncan_dict(obj, g=None):
    if istype(obj, dict):
        newobj = {}
        for k, v in obj.items():
            newobj[k] = uncan(v,g)
        return newobj
    else:
        return obj

def uncan_sequence(obj, g=None):
    if istype(obj, sequence_types):
        t = type(obj)
        return t([uncan(i,g) for i in obj])
    else:
        return obj

#-------------------------------------------------------------------------------
# API dictionaries
#-------------------------------------------------------------------------------

# These dicts can be extended for custom serialization of new objects

can_map = {
    'numpy.ndarray' : CannedArray,
    FunctionType : CannedFunction,
    bytes : CannedBytes,
    memoryview : CannedMemoryView,
    cell_type : CannedCell,
    class_type : can_class,
}
if buffer is not memoryview:
    can_map[buffer] = CannedBuffer

uncan_map = {
    CannedObject : lambda obj, g: obj.get_object(g),
    dict : uncan_dict,
}

# for use in _import_mapping:
_original_can_map = can_map.copy()
_original_uncan_map = uncan_map.copy()
