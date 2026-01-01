#!/usr/bin/env python3

"""
Provides base classes and utilities for defining database models using Pydantic and SQLAlchemy.
"""

import sys

from abc import ABC
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from functools import cache, cached_property
from types import MappingProxyType
from typing import Annotated, Any, ClassVar, ForwardRef, Never, NewType, TypeGuard, overload

import sqlalchemy

from frozendict import frozendict
from more_itertools import one, only
from pydantic import BaseModel, HttpUrl, JsonValue, PlainSerializer, ValidatorFunctionWrapHandler, WrapValidator
from pydantic.fields import ComputedFieldInfo, FieldInfo
from pydantic_extra_types.country import CountryNumericCode
from sqlalchemy import Column, ForeignKey, MetaData, Table
from sqlalchemy.sql.base import SchemaEventTarget
from sqlalchemy.types import TypeEngine
from sqlalchemy.util.typing import (GenericProtocol, TypeAliasType,
                                    de_optionalize_union_types,
                                    eval_expression, flatten_newtype, get_args,
                                    includes_none, is_fwd_ref, is_generic,
                                    is_literal, is_newtype, is_pep593, is_pep695, make_union_type)

type AnnotationScanType = type[Any] | str | ForwardRef | NewType | TypeAliasType | GenericProtocol[Any]
type TupleOf[T] = tuple[T, ...]

@overload
def get_default_column_type(annotation: None, module_name: str | None = ..., globalns: dict[str, Any] | None = ...) -> Never: ...

@overload
def get_default_column_type(annotation: AnnotationScanType, module_name: str | None = ..., globalns: dict[str, Any] | None = ...) -> type[TypeEngine] | ForeignKey: ...

def get_default_column_type(annotation: AnnotationScanType | None, module_name: str | None = None, globalns: dict[str, Any] | None = None) -> type[TypeEngine] | ForeignKey:
    """
    Maps a Pydantic type annotation to a SQLAlchemy column type.
    """
    if annotation is None:
        raise TypeError("Cannot determine column type for field with no type annotation")

    field_type = unwrap_type(annotation, module_name, globalns)

    if field_type == bool:
        return sqlalchemy.Boolean

    if issubclass(field_type, (int, CountryNumericCode)):
        return sqlalchemy.Integer

    if issubclass(field_type, (str, HttpUrl)):
        return sqlalchemy.String

    if field_type == datetime:
        return sqlalchemy.DateTime

    if field_type == date:
        return sqlalchemy.Date

    if field_type == float:
        return sqlalchemy.Float

    if field_type == JsonValue:
        return sqlalchemy.JSON

    if issubclass(field_type, DatabaseModel):
        # If this field refers to a specific model, create a ForeignKey to that model's table
        colname = one(
            field_type.pk_columns(),
            too_short=ValueError(f"Cannot create a default ForeignKey for {field_type.__name__} because it has no primary key columns (try defining one explicitly)"),
            too_long=ValueError(f"Cannot create a default ForeignKey for {field_type.__name__} because it has multiple primary key columns (composite foreign keys are not supported yet)"),
        )
        # one() raises if its argument doesn't have exactly one item

        return ForeignKey(f"{field_type.__tablename__}.{colname}")

    raise NotImplementedError(f"Unsupported field type: {field_type}")

def is_non_string_iterable_type(t: type[Any]) -> TypeGuard[type[Iterable[Any]]]:
    """
    Determines whether a type annotation represents a non-string iterable type.
    """
    if issubclass(t, Sequence) and not issubclass(t, (str, bytes, bytearray)):
        return True

    return False

# TODO: I need to simplify this
def unwrap_type(
    t: AnnotationScanType,
    name: str | None = None,
    globalns: dict[str, Any] | None = None,
) -> type:
    """
    Strips away literals, optionals, newtypes, generics, and forward references.

    :param t: The type annotation to unwrap
    :param name: Module name for resolving forward references (defaults to this module's __name__)
    :param globalns: Global namespace dict for evaluating expressions (defaults to this module's globals)
    """
    if globalns is None:
        globalns = sys.modules[name or __name__].__dict__

    match t:
        case type() as concrete_type:
            return concrete_type
        case optional if includes_none(t):
            # If this type can have a value of None...
            # (For most purposes you can think of it as Optional[], but
            # Python has several ways to express that.)
            return unwrap_type(de_optionalize_union_types(t), name, globalns)
        case alias if is_pep695(alias) and (args := get_args(alias)):
            # If this is a type alias with parameters...
            return unwrap_type(args[0], name, globalns)
        case alias if is_pep695(alias):
            # If this is a plain type alias...
            return unwrap_type(alias.__value__, name, globalns)
        case literal if is_literal(literal):
            # If this is a Literal[...], unwrap to get the argument types
            args = get_args(literal)
            literal_types = set(map(type, args))

            return type(args[0]) if len(literal_types) == 1 else make_union_type(*literal_types)
        case annotation if is_pep593(annotation):
            # If this is Annotated[T, ...], unwrap to get T
            args = get_args(annotation)
            return unwrap_type(args[0], name, globalns)
        case newtype if is_newtype(newtype):
            # If this is a newtype, unwrap to get the underlying type
            return unwrap_type(flatten_newtype(newtype))
        case generic if is_generic(generic):
            # If this is a generic type (likely a collection), unwrap to get the first argument
            return unwrap_type(get_args(generic)[0], name, globalns)
        case ref if is_fwd_ref(ref, check_generic=True, check_for_plain_string=True):
            return unwrap_type(eval_expression(ref.__forward_arg__, name or __name__, locals_=globalns), name, globalns)
        case str() as type_expression:
            return unwrap_type(eval_expression(type_expression, name or __name__, locals_=globalns), name, globalns)

        case _:
            raise TypeError(f"Unexpected type annotation: {t} ({type(t)})")


@dataclass(kw_only=True, eq=True)
class ColumnDef:
    # TODO: Write about how I tried to use SQLModel but it was a pain in the ass
    name: str | None
    coltype: type[TypeEngine] | TypeEngine | SchemaEventTarget | None
    primary_key: bool
    index: bool | None
    unique: bool | None
    nullable: bool | None
    kwargs: dict[str, Any]
    args: tuple[Any, ...]

    def __init__(
            self,
            *args,
            name: str | None = None,
            type: type[TypeEngine] | TypeEngine | SchemaEventTarget | None = None,
            index: bool | None = None,
            unique: bool | None = None,
            nullable: bool | None = None,
            primary_key: bool = False,
            **kwargs: Any,
    ):
        self.name = name
        self.coltype = type
        self.primary_key = primary_key
        self.index = index
        self.unique = unique
        self.nullable = nullable
        self.args = args
        self.kwargs = kwargs


class RelationshipDef:
    """
    Composite foreign keys are not yet supported.
    """

    foreign_key: ForeignKey
    foreign_colname: str

    def __init__(self,
        spec: ForeignKey | str | type["DatabaseModel"] | ForwardRef,
        foreign_colname: str | None = None,
        **kwargs
    ) -> None:
        match spec:
            case ForeignKey():
                self.foreign_key = spec
                self.foreign_colname = foreign_colname or "foreign_pk"
            case str() as s:
                self.foreign_key = ForeignKey(s, **kwargs)
                self.foreign_colname = foreign_colname or "foreign_pk"
            case ForwardRef():
                foreign_type = eval_expression(spec.__forward_arg__, __name__)
                if not issubclass(foreign_type, DatabaseModel):
                    raise TypeError(f"{spec} does not refer to a DatabaseModel subclass")

                # one() raises if its argument doesn't have exactly one item
                foreign_pk_colname = one(foreign_type.pk_columns())
                self.foreign_key = ForeignKey(f"{foreign_type.__tablename__}.{foreign_pk_colname}", **kwargs)
                self.foreign_colname = foreign_colname or f"{foreign_type.__tablename__}_{foreign_pk_colname}"
            case type() as foreign_type if issubclass(foreign_type, DatabaseModel):
                foreign_pk_colname = one(foreign_type.pk_columns())
                self.foreign_key = ForeignKey(f"{foreign_type.__tablename__}.{foreign_pk_colname}", **kwargs)
                self.foreign_colname = foreign_colname or f"{foreign_type.__tablename__}_{foreign_pk_colname}"
            case _:
                raise TypeError(f"Expected a ForeignKey, str, ForwardRef, or type[DatabaseModel]: got {type(spec)} ({spec})")

    def create_table(self, metadata: MetaData, parent_type: type["DatabaseModel"], parent_field_name: str) -> Table:
        """
        Creates a relationship table according to the following conventions:

        - Composite foreign key relationships aren't supported, an exception will be raised if attempted
        - The table is named `<parent tablename>_<field name>`
        - The parent's primary key is referenced as a foreign key column named `<parent tablename>_<parent pk column name>`
        - The child's primary key column is referenced as a foreign key column named `<child_tablename>_<field name>`
          (unless `foreign_colname` was specified in __init__(), in which case that name is used instead)
        """
        parent_pk_colname = one(parent_type.pk_columns())

        return Table(
            f"{parent_type.__tablename__}_{parent_field_name}",
            metadata,
            Column(
                f"{parent_type.__tablename__}_{parent_pk_colname}",
                ForeignKey(f"{parent_type.__tablename__}.{parent_pk_colname}"),
                primary_key=True
            ),
            Column(
                self.foreign_colname,
                self.foreign_key,
                primary_key=True
            ),
        )

SchemaDef = ColumnDef | RelationshipDef

class DatabaseModel(BaseModel, ABC, frozen=True):
    __tablename__: ClassVar[str]

    @classmethod
    @cache
    def pk_columns(cls) -> dict[str, ColumnDef]:
        """
        Returns a dictionary of primary key columns for the model.

        :return: A dictionary mapping field names to ColumnDef instances that are primary keys.
        """

        result: dict[str, ColumnDef] = {}
        for field_name, field in cls.model_fields.items():
            defn = only(m for m in field.metadata if isinstance(m, ColumnDef))
            if defn and defn.primary_key:
                result[field_name] = defn

        return result

    @classmethod
    @cache
    def relationship_defs(cls) -> dict[str, RelationshipDef]:
        """
        Gets all RelationshipDef instances from this model type's fields,
        as defined in their Annotated metadata.
        Returns a map of field names to RelationshipDef instances,
        empty if none are found.

        :raises ValueError: if multiple RelationshipDefs are found on a single field.
        """

        result: dict[str, RelationshipDef] = {}
        for field_name, field in cls.model_fields.items():
            defn = only(m for m in field.metadata if isinstance(m, RelationshipDef))
            if defn:
                result[field_name] = defn

        return result

    @classmethod
    def create_column(cls, field_name: str, coldef: ColumnDef | None = None) -> Column:
        """
        Creates a SQLAlchemy Column object from a ColumnDef for this model type.

        :param coldef: The ColumnDef to create the Column from.
        :param field_name: The name of the field in the model corresponding to this column.
        :return: A SQLAlchemy Column object.
        """
        field = cls.model_fields.get(field_name) or cls.model_computed_fields.get(field_name)
        match field:
            case FieldInfo(annotation=None):
                raise TypeError(f"{cls.__name__}.{field_name!r} has no type annotation, cannot generate column definition")
            case FieldInfo(annotation=annotation):
                if not coldef:
                    coldef = only((m for m in field.metadata if isinstance(m, ColumnDef)), default=ColumnDef())
                nullable = coldef.nullable if coldef.nullable is not None else includes_none(annotation)
                coltype = coldef.coltype or get_default_column_type(annotation, cls.__module__, sys.modules[cls.__module__].__dict__)
            case ComputedFieldInfo(return_type=None):
                raise TypeError(f"{cls.__name__}.{field_name!r} has no return type annotation, cannot generate column definition")
            case ComputedFieldInfo(return_type=annotation) if is_pep593(annotation):
                # Create a column for a computed field whose return type is an Annotated
                args = get_args(annotation)
                if not coldef:
                    coldef = only((m for m in args if isinstance(m, ColumnDef)), default=ColumnDef())
                return_type = args[0]
                nullable = coldef.nullable if coldef.nullable is not None else includes_none(return_type)
                coltype = coldef.coltype or get_default_column_type(return_type, cls.__module__, sys.modules[cls.__module__].__dict__)
            case ComputedFieldInfo(return_type=return_type):
                # Create a column for a computed field whose return type is not an Annotated
                if not coldef:
                    coldef = ColumnDef()

                nullable = coldef.nullable if coldef.nullable is not None else includes_none(return_type)
                coltype = coldef.coltype or get_default_column_type(return_type, cls.__module__, sys.modules[cls.__module__].__dict__)
            case None:
                raise KeyError(f"Model {cls.__name__} has no real or computed field named {field_name!r}")
            case _:
                raise TypeError(f"Expected FieldInfo or ComputedFieldInfo for {cls.__name__}.{field_name!r}, got {type(field)}")

        return Column(
            coldef.name or field_name,
            coltype,
            *coldef.args,
            primary_key=coldef.primary_key,
            index=coldef.index,
            unique=coldef.unique,
            nullable=nullable,
            **coldef.kwargs,
        )

    @classmethod
    def create_tables(cls, metadata: MetaData) -> tuple[Table, *tuple[Table, ...]]:
        """
        Creates a SQLAlchemy Table object for this model type,
        and any associated relationship tables.

        Returns a tuple where the first item is the main table,
        and any subsequent items are relationship tables.
        """
        main_table = Table(cls.__tablename__, metadata)
        relationship_tables: list[Table] = []

        def handle_schemadef(name: str, annotation: AnnotationScanType, defn: SchemaDef | None):
            unwrapped_type = unwrap_type(annotation, cls.__module__, sys.modules[cls.__module__].__dict__)
            match defn:
                # only() raises if its argument has more than one item
                case ColumnDef() as coldef:
                    # Create a Column with the specified ColumnDef
                    main_table.append_column(cls.create_column(name, coldef))
                case RelationshipDef() as reldef:
                    # An explicit RelationshipDef was provided, so use it
                    relationship_tables.append(reldef.create_table(metadata, cls, name))
                case None if is_non_string_iterable_type(unwrapped_type):
                    # This field is a collection of related DatabaseModel instances
                    arg_type = get_args(annotation)[0]
                    unwrapped_arg_type = unwrap_type(arg_type, cls.__module__, sys.modules[cls.__module__].__dict__)
                    reldef = RelationshipDef(unwrapped_arg_type)
                    relationship_tables.append(reldef.create_table(metadata, cls, name))
                case None:
                    # No SchemaDef was provided, create a ColumnDef with defaults
                    main_table.append_column(cls.create_column(name))
                case other:
                    raise TypeError(f"Expected zero or one SchemaDef on {cls.__name__}.{name!r}, got {other}")

        for field_name, field in cls.model_fields.items():
            if field.exclude:
                # Excluded fields won't have columns
                continue

            annotation: AnnotationScanType | None = field.annotation
            if annotation is None:
                raise TypeError(f"{cls.__name__}.{field_name!r} has no type annotation, cannot generate column definition")

            schemadef = only(m for m in field.metadata if isinstance(m, SchemaDef))
            handle_schemadef(field_name, annotation, schemadef)

        for computed_field_name, computed_field in cls.model_computed_fields.items():
            return_type = computed_field.return_type
            if return_type is None:
                raise TypeError(f"{cls.__name__}.{computed_field_name!r} has no return type annotation, cannot generate column definition")

            # Get the explicitly-defined SchemaDef if the return type is an Annotated,
            # otherwise use the default behavior.
            args = get_args(return_type) if is_pep593(return_type) else ()
            schemadef = only(m for m in args if isinstance(m, SchemaDef))
            handle_schemadef(computed_field_name, args[0] if args else return_type, schemadef)

        return (main_table, *relationship_tables)


    @cached_property
    def nested_models(self) -> frozenset["DatabaseModel"]:
        """
        Returns a set of all nested DatabaseModel instances referenced by this model's fields,
        excluding itself.
        """
        models: set[DatabaseModel] = set()
        for field_name in type(self).model_fields:
            match getattr(self, field_name):
                case DatabaseModel() as obj:
                    models.add(obj)
                    models.update(obj.nested_models)
                case [*items]:
                    objects = (i for i in items if isinstance(i, DatabaseModel))
                    for obj in objects:
                        models.add(obj)
                        models.update(obj.nested_models)

        return frozenset(models)

    @cached_property
    def relationships(self) -> Mapping[str, list[dict[str, Any]]]:
        """
        Returns a dictionary whose keys are field names representing relationships,
        and whose values are sets of dicts suitable for relationship tables.
        These dicts include the foreign key mappings for this object and the related objects.

        This property is not recursive, i.e. it does not include relationships from nested models.
        """
        results: dict[str, list[dict[str, Any]]] = defaultdict(list)
        cls = type(self)
        pk_coldefs = cls.pk_columns()
        reldefs = cls.relationship_defs()

        for field_name, field_info in cls.model_fields.items():
            if field_name not in reldefs:
                continue

            reldef = reldefs[field_name]
            foreign_colname = reldef.foreign_colname

            match getattr(self, field_name):
                case DatabaseModel() as related_obj:
                    mapping: dict[str, Any] = {}
                    for pk_field_name, pk_coldef in pk_coldefs.items():
                        mapping[f"{cls.__tablename__}_{pk_coldef.name or pk_field_name}"] = getattr(self, pk_field_name)

                    related_pk_coldefs = type(related_obj).pk_columns()
                    for related_pk_field_name, related_pk_coldef in related_pk_coldefs.items():
                        mapping[foreign_colname] = getattr(related_obj, related_pk_field_name)

                    results[field_name].append(mapping)
                case [*related_objs]:
                    for related_obj in related_objs:
                        if not isinstance(related_obj, DatabaseModel):
                            continue

                        mapping = {}
                        for pk_field_name, pk_coldef in pk_coldefs.items():
                            mapping[f"{cls.__tablename__}_{pk_coldef.name or pk_field_name}"] = getattr(self, pk_field_name)

                        related_pk_coldefs = type(related_obj).pk_columns()
                        for related_pk_field_name, related_pk_coldef in related_pk_coldefs.items():
                            mapping[foreign_colname] = getattr(related_obj, related_pk_field_name)

                        results[field_name].append(mapping)
                case _:
                    continue

        return MappingProxyType(results)

type CoercedHttpUrl = Annotated[HttpUrl, WrapValidator(lambda v, h: h(v) if v else None), PlainSerializer(str, str)]

def validate_frozendict(v: Any, handler: ValidatorFunctionWrapHandler) -> frozendict[Any, Any]:
    if isinstance(v, frozendict):
        return v

    if isinstance(v, Mapping):
        return frozendict(handler(v))

    raise TypeError(f"Expected frozendict or Mapping, got {type(v)}")

FrozenDictValidator = WrapValidator(validate_frozendict)

type FrozenDict[K, V] = Annotated[frozendict[K, V], FrozenDictValidator]

__all__ = (
    "ColumnDef",
    "DatabaseModel",
    "RelationshipDef",
    "get_default_column_type",
    "is_non_string_iterable_type",
    "unwrap_type",
    "TupleOf",
    "CoercedHttpUrl",
    "FrozenDictValidator"
)