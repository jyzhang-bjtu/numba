# -*- coding: utf-8 -*-

"""

"""

from __future__ import print_function, division, absolute_import

import struct

from numba.typesystem.typesystem import (
    Universe, Type, Conser, nbo, ConstantTyper, TypeConverter)
from numba.typesystem.kinds import *

misc_typenames = [
    'c_string_type', 'object_', 'void', 'struct',
]

#------------------------------------------------------------------------
# User-facing type functionality
#------------------------------------------------------------------------

class NumbaType(Type):
    """
    MonoType with user-facing methods:

        call: create a function type
        slice: create an array type
        conversion: to_llvm/to_ctypes/get_dtype
    """

    # TODO: For methods pointer, __getitem__ and __call__ we need the type
    # TODO: universe. Should each type instance hold the type universe it
    # TODO: was constructed in?

    def __getitem__(self, item):
        """
        Support array type creation by slicing, e.g. double[:, :] specifies
        a 2D strided array of doubles. The syntax is the same as for
        Cython memoryviews.
        """
        assert isinstance(item, (tuple, slice))

        def verify_slice(s):
            if s.start or s.stop or s.step not in (None, 1):
                raise ValueError(
                    "Only a step of 1 may be provided to indicate C or "
                    "Fortran contiguity")

        if isinstance(item, tuple):
            step_idx = None
            for idx, s in enumerate(item):
                verify_slice(s)
                if s.step and (step_idx or idx not in (0, len(item) - 1)):
                    raise ValueError(
                        "Step may only be provided once, and only in the "
                        "first or last dimension.")

                if s.step == 1:
                    step_idx = idx

            return type(self)(self.dtype, len(item),
                              is_c_contig=step_idx == len(item) - 1,
                              is_f_contig=step_idx == 0)
        else:
            verify_slice(item)
            return ArrayType(type, 1, is_c_contig=bool(item.step))

    def __call__(self, *args):
        """
        Return a new function type when called with type arguments.
        """
        if len(args) == 1 and not isinstance(args[0], Type):
            # Cast in Python space
            # TODO: Create proxy object
            # TODO: Fully customizable type system (do this in Numba, not
            #       minivect)
            return args[0]

        return FunctionType(self, args)

def make_polytype(kind, names, defaults=()):
    """
    Create a new polytype that has named attributes. E.g.

        make_polytype("array", ["dtype", "ndim"])
    """
    def __init__(self, *params):
        assert len(params) == len(names), polyctor
        super(polyctor, self).__init__(kind, params)

    @classmethod
    def default_args(cls, args, kwargs):
        if len(args) == len(names):
            return args

        # Insert defaults in args tuple
        args = list(args)
        for name in names[len(args):]:
            if name in kwargs:
                args.append(kwargs[name])
            elif name in defaults:
                args.append(defaults[name])
            else:
                raise TypeError(
                    "Constructor '%s' requires %d arguments (got %d)" % (
                                            kind, len(names), len(args)))

        return tuple(args)

    # Create parameter accessors
    typedict = dict([(name, property(lambda self, i=i: self.params[i]))
                         for i, name in enumerate(names)])
    typedict["__init__"] = __init__
    typedict["default_args"] = default_args

    polyctor = type(kind, (NumbaType,), typedict)
    return polyctor

#------------------------------------------------------------------------
# Type constructors
#------------------------------------------------------------------------

class ArrayType(make_polytype(KIND_ARRAY, ["dtype", "ndim"])):
    """
    An array type. ArrayType may be sliced to obtain a subtype:

    >>> double[:, :, ::1][1:]
    double[:, ::1]
    >>> double[:, :, ::1][:-1]
    double[:, :]

    >>> double[::1, :, :][:-1]
    double[::1, :]
    >>> double[::1, :, :][1:]
    double[:, :]
    """

    def pointer(self):
        raise Exception("You probably want a pointer type to the dtype")

    def __repr__(self):
        axes = [":"] * self.ndim
        if self.is_c_contig and self.ndim > 0:
            axes[-1] = "::1"
        elif self.is_f_contig and self.ndim > 0:
            axes[0] = "::1"

        return "%s[%s]" % (self.dtype, ", ".join(axes))

    def __getitem__(self, index):
        "Slicing an array slices the dimensions"
        assert isinstance(index, slice)
        assert index.step is None
        assert index.start is not None or index.stop is not None

        start = 0
        stop = self.ndim
        if index.start is not None:
            start = index.start
        if index.stop is not None:
            stop = index.stop

        ndim = len(range(self.ndim)[start:stop])

        if ndim == 0:
            return self.dtype
        elif ndim > 0:
            return type(self)(self.dtype, ndim)
        else:
            raise IndexError(index, ndim)


class PointerType(make_polytype(KIND_POINTER, ["base_type"])):
    def __repr__(self):
        space = " " * (not self.base_type.is_pointer)
        return "%s%s*" % (self.base_type, space)

class CArrayType(make_polytype(KIND_CARRAY, ["base_type", "size"])):
    def __repr__(self):
        return "%s[%d]" % (self.base_type, self.size)

# ______________________________________________________________________
# Function

def pass_by_ref(type):
    return type.is_struct or type.is_complex

class Function(object):
    """
    Function types may be called with Python functions to create a Function
    object. This may be used to minivect users for their own purposes. e.g.

    @double(double, double)
    def myfunc(...):
       ...
    """

    def __init__(self, signature, py_func):
        self.signature = signature
        self.py_func = py_func

    def __call__(self, *args, **kwargs):
        """
        Implement this to pass the callable test for classmethod/staticmethod.
        E.g.

            @classmethod
            @void()
            def m(self):
                ...
        """
        raise TypeError("Not a callable function")

_FunctionType = make_polytype(
    KIND_FUNCTION,
    ['return_type', 'args', 'name', 'is_vararg'],
    defaults={"name": None, "is_vararg": False},
)

class FunctionType(_FunctionType):

    struct_by_reference = False

    def __repr__(self):
        args = [str(arg) for arg in self.args]
        if self.is_vararg:
            args.append("...")
        if self.name:
            namestr = self.name
        else:
            namestr = ''

        return "%s (*%s)(%s)" % (self.return_type, namestr, ", ".join(args))

    # @property
    # def actual_signature(self):
    #     """
    #     Passing structs by value is not properly supported for different
    #     calling conventions in LLVM, so we take an extra argument
    #     pointing to a caller-allocated struct value.
    #     """
    #     if self.struct_by_reference:
    #         args = []
    #         for arg in self.args:
    #             if pass_by_ref(arg):
    #                 arg = arg.pointer()
    #             args.append(arg)
    #
    #         return_type = self.return_type
    #         if pass_by_ref(self.return_type):
    #             return_type = void
    #             args.append(self.return_type.pointer())
    #
    #         self = FunctionType(return_type, args)
    #
    #     return self

    @property
    def struct_return_type(self):
        # Function returns a struct.
        return self.return_type.pointer()

    def __call__(self, *args):
        if len(args) != 1 or isinstance(args[0], Type):
            return super(FunctionType, self).__call__(*args)

        assert self.return_type is not None
        assert self.args is not None
        func, = args
        return Function(self, func)

# ______________________________________________________________________
# Structs

_StructType = make_polytype(
    KIND_STRUCT,
    ["fields", "name", "readonly", "packed"],
    defaults={"name": None, "readonly": False, "packed": False})

class StructType(_StructType):
    """
    Create a struct type. Fields may be ordered or unordered. Unordered fields
    will be ordered from big types to small types (for better alignment).

    >>> struct([('a', int_), ('b', float_)], name='Foo') # ordered struct
    struct Foo { int a, float b }
    >>> struct(a=int_, b=float_, name='Foo') # unordered struct
    struct Foo { float b, int a }
    >>> struct(a=int32, b=int32, name='Foo') # unordered struct
    struct Foo { int32 a, int32 b }

    >>> S = struct(a=complex128, b=complex64, c=struct(f1=double, f2=double, f3=int32))
    >>> S
    struct { struct { double f1, double f2, int32 f3 } c, complex128 a, complex64 b }

    >>> S.offsetof('a')
    24
    """

    @property
    def fielddict(self):
        return dict(self.fields)

    def copy(self):
        return self.ts.struct(self.fields, self.name, self.readonly, self.packed)

    def __repr__(self):
        if self.name:
            name = self.name + ' '
        else:
            name = ''
        return 'struct %s{ %s }' % (
                name, ", ".join("%s %s" % (field_type, field_name)
                                    for field_name, field_type in self.fields))

    def __eq__(self, other):
        return other.is_struct and self.fields == other.fields

    def __hash__(self):
        return hash(tuple(self.fields))

    def is_prefix(self, other_struct):
        other_fields = other_struct.fields[:len(self.fields)]
        return self.fields == other_fields

    def add_field(self, name, type):
        assert name not in self.fielddict
        self.fielddict[name] = type
        self.fields.append((name, type))
        self.mutated = True

    def update_mutated(self):
        self.rank = sum([_sort_key(field) for field in self.fields])
        self.mutated = False

    def offsetof(self, field_name):
        """
        Compute the offset of a field. Must be used only after mutation has
        finished.
        """
        ctype = self.to_ctypes()
        return getattr(ctype, field_name).offset

#------------------------------------------------------------------------
# Type Ordering
#------------------------------------------------------------------------

def _sort_types_key(field_type):
    if field_type.is_complex:
        return field_type.base_type.rank * 2
    elif field_type.is_numeric or field_type.is_struct:
        return field_type.rank
    elif field_type.is_vector:
        return _sort_types_key(field_type.element_type) * field_type.vector_size
    elif field_type.is_carray:
        return _sort_types_key(field_type.base_type) * field_type.size
    elif field_type.is_pointer or field_type.is_object or field_type.is_array:
        return 8
    else:
        return 1

def _sort_key(keyvalue):
    field_name, field_type = keyvalue
    return _sort_types_key(field_type)

def sort_types(types_dict):
    # reverse sort on rank, forward sort on name
    d = {}
    for field in types_dict.iteritems():
        key = _sort_key(field)
        d.setdefault(key, []).append(field)

    def key(keyvalue):
        field_name, field_type = keyvalue
        return field_name

    fields = []
    for rank in sorted(d, reverse=True):
        fields.extend(sorted(d[rank], key=key))

    return fields


if __name__ == '__main__':
    import doctest
    doctest.testmod()