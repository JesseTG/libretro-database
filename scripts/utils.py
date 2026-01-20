#!/usr/bin/env python3

"""
Provides base classes and utilities for defining database models using Pydantic and SQLAlchemy.
"""

import sys

from abc import ABC
from collections import defaultdict
from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import date, datetime
from functools import cache, cached_property
from itertools import chain
from types import MappingProxyType
from typing import Annotated, Any, ClassVar, ForwardRef, Literal, NewType, Self, TypeGuard, get_origin

import sqlalchemy

from more_itertools import always_iterable, one, only
from pydantic import BaseModel, BeforeValidator, HttpUrl, JsonValue, PlainSerializer, StringConstraints, ValidatorFunctionWrapHandler, WrapSerializer, WrapValidator
from pydantic.fields import ComputedFieldInfo, FieldInfo
from pydantic_extra_types.country import CountryNumericCode
from sqlalchemy import Column, ForeignKey, MetaData, Table
from sqlalchemy.schema import SchemaConst, SchemaItem
from sqlalchemy.types import NullType, TypeEngine
from sqlalchemy.util import EMPTY_DICT, immutabledict
from sqlalchemy.util.typing import (GenericProtocol, TypeAliasType,
                                    de_optionalize_union_types,
                                    eval_expression, flatten_newtype, get_args,
                                    includes_none, is_fwd_ref, is_generic,
                                    is_literal, is_newtype, is_pep593, is_pep695, make_union_type)

type AnnotationScanType = type[Any] | str | ForwardRef | NewType | TypeAliasType | GenericProtocol[Any]

def is_non_string_sequence_type(t: Any) -> TypeGuard[type[Sequence[Any]]]:
    """
    Determines whether a type annotation represents a non-string iterable type.
    """
    if isinstance(t, type) and issubclass(t, Sequence) and not issubclass(t, (str, bytes, bytearray)):
        return True

    return False

class CopyableSchemaItem(SchemaItem, ABC):
    """
    A SchemaItem that can be copied.
    Use as a type annotation.
    """

    def _copy(self, **kwargs) -> Self:
        ...

@dataclass(eq=True, unsafe_hash=True)
class RelationshipTableDef:

    tablename: str | None = None
    """
    The name of the relationship table that will be created
    to represent this relationship.

    If None, a default name will be generated based on the parent model's table name
    and the Pydantic field name that this RelationshipTableDef is associated with.
    """

    self_columns: tuple[Column, ...] = ()
    """
    One or more `Column`s that identify the "parent" object.
    """

    related_columns: tuple[Column, ...] = ()
    """
    One or more `Column`s that define the related object.

    Can be references to another `DatabaseModel`'s primary key columns,
    or primitive values.
    """

    tableargs: tuple[CopyableSchemaItem, ...] = ()
    """
    Positional arguments to pass as-is to the Table constructor
    after the table name, metadata, and explicit constraints.
    Useful for table-level constraints.
    """

    tablekwargs: immutabledict[str, Any] = field(default_factory=lambda: EMPTY_DICT)
    """
    Keyword arguments to pass as-is to the Table constructor.
    """

    def __deepcopy__(self, memo: dict[int, Any]) -> "RelationshipTableDef":
        return RelationshipTableDef(
            tablename=self.tablename,
            self_columns=tuple(c._copy() for c in self.self_columns),
            related_columns=tuple(c._copy() for c in self.related_columns),
            tableargs=tuple(i._copy() for i in self.tableargs),
            tablekwargs=immutabledict(self.tablekwargs),
        )

SchemaDef = Column | RelationshipTableDef

class DatabaseModel(BaseModel, ABC, frozen=True):
    __tablename__: ClassVar[str]
    __tableconstraints__: ClassVar[tuple[SchemaItem, ...]] = ()
    __tablekwargs__: ClassVar[Mapping[str, Any]] = EMPTY_DICT

    @classmethod
    def get_default_column_type(cls, annotation: AnnotationScanType | ComputedFieldInfo | FieldInfo) -> type[TypeEngine]:
        """
        Maps a Pydantic type annotation to a SQLAlchemy column type.

        :param annotation: A type declaration, or a FieldInfo or ComputedFieldInfo instance.
        :return: A SQLAlchemy TypeEngine subclass representing the column type.
        """

        if isinstance(annotation, FieldInfo):
            if annotation.annotation is None:
                raise TypeError("Field has no type annotation")

            return cls.get_default_column_type(annotation.annotation)

        if isinstance(annotation, ComputedFieldInfo):
            if annotation.return_type is None:
                raise TypeError("Computed field has no return type annotation")

            return cls.get_default_column_type(annotation.return_type)

        field_type = cls.unwrap_type(annotation)

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

        raise NotImplementedError(f"Unsupported field type: {field_type}")

    @classmethod
    @cache
    def get_default_foreign_keys(cls) -> tuple[ForeignKey, ...]:
        """
        Returns foreign keys referencing this model's primary key columns.
        The returned keys can be used to uniquely identify an instance of this model.

        :return: A tuple of `ForeignKey` instances, one for each primary key column.
        Usually just one, but can be more for composite primary keys.
        """

        fk_items: list[ForeignKey] = []
        pk_cols = cls.pk_columns()
        for pk_field_name, pk_coldef in pk_cols.items():
            fk_items.append(
                ForeignKey(f"{cls.__tablename__}.{pk_coldef.name or pk_field_name}")
            )

        return tuple(fk_items)

    @classmethod
    @cache
    def unwrap_type(
        cls,
        t: AnnotationScanType
    ) -> type:
        """
        Strips away literals, optionals, newtypes, generics, and forward references.

        :param t: The type annotation to unwrap
        """
        match t:
            case type() as concrete_type:
                return concrete_type
            case alias if is_pep695(alias) and (args := get_args(alias)):
                # If this is a type alias with parameters...
                return cls.unwrap_type(args[0])
            case alias if is_pep695(alias):
                # If this is a plain type alias...
                return cls.unwrap_type(alias.__value__)
            case literal if is_literal(literal):
                # If this is a Literal[...], unwrap to get the argument types
                args = get_args(literal)
                literal_types = set(map(type, args))

                return type(args[0]) if len(literal_types) == 1 else make_union_type(*literal_types)
            case annotation if is_pep593(annotation):
                # If this is Annotated[T, ...], unwrap to get T
                args = get_args(annotation)
                return cls.unwrap_type(args[0])
            case newtype if is_newtype(newtype):
                # If this is a newtype, unwrap to get the underlying type
                return cls.unwrap_type(flatten_newtype(newtype))
            case generic if is_generic(generic):
                # If this is a generic type (likely a collection), unwrap to get the first argument
                return cls.unwrap_type(get_args(generic)[0])
            case ref if is_fwd_ref(ref, check_generic=True, check_for_plain_string=True):
                return cls.unwrap_type(eval_expression(ref.__forward_arg__, cls.__module__, locals_=sys.modules[cls.__module__].__dict__))
            case str() as type_expression:
                return cls.unwrap_type(eval_expression(type_expression, cls.__module__, locals_=sys.modules[cls.__module__].__dict__))
            case optional if includes_none(t):
                # If this type can have a value of None...
                # (For most purposes you can think of it as Optional[], but
                # Python has several ways to express that.)
                return cls.unwrap_type(de_optionalize_union_types(t))
            case _:
                raise TypeError(f"Unexpected type annotation: {t} ({type(t)})")

    @classmethod
    def get_field_type(cls, field: FieldInfo | ComputedFieldInfo | str) -> type:
        field_annotation = cls.get_field_annotation(field)
        if field_annotation is None:
            raise TypeError(f"Model field {cls.__name__}.{field} has no type annotation")

        return cls.unwrap_type(field_annotation)

    @classmethod
    def get_field_annotation(cls, field: FieldInfo | ComputedFieldInfo | str) -> AnnotationScanType | None:
        """
        Returns the type annotation of a model field.

        :param field: A `FieldInfo`, `ComputedFieldInfo`, or field name.
        """
        match field:
            case str(field_name):
                info = cls.model_fields.get(field_name) or cls.model_computed_fields.get(field_name)
                if info is None:
                    raise KeyError(f"{cls.__name__} has no real or computed Pydantic field named {field_name!r}")
                return cls.get_field_annotation(info)
            case FieldInfo(annotation=annotation):
                return annotation
            case ComputedFieldInfo(return_type=annotation):
                return annotation
            case _:
                raise TypeError(f"Expected FieldInfo, ComputedFieldInfo, or str; got {type(field)}")

    @classmethod
    def get_collection_element_type(cls, field: FieldInfo | ComputedFieldInfo | str) -> type:
        """
        Returns the element type of a collection-typed model field.

        :param field: A `FieldInfo`, `ComputedFieldInfo`, or field name.
        :return: The element type of the collection.
        :raises TypeError: if the field is not a collection type.
        """

        field_type = cls.get_field_type(field)
        if not is_non_string_sequence_type(field_type):
            raise TypeError(f"Field {cls.__name__}.{field} is not a non-string sequence type; got {field_type}")

        element_type = cls.unwrap_type(get_args(field_type)[0])
        return element_type

    @classmethod
    def get_column_metadata(cls, field: FieldInfo | ComputedFieldInfo | str) -> Column | None:
        """
        Retrieves a copy of the `Column` explicitly defined on a model field's `Annotated` metadata, if any.

        :param field: A `FieldInfo`, `ComputedFieldInfo`, or field name.
        :return: The `Column` defined on the field, or `None` if there isn't one.
        :raises KeyError: if the field name does not exist on this model.
        :raises TypeError: if the field is not one of the expected types.
        :raises ValueError: if multiple `Column` instances are defined on the field.
        """

        match field:
            case str(field_name):
                info = cls.model_fields.get(field_name) or cls.model_computed_fields.get(field_name)
                if info is None:
                    raise KeyError(f"Model {cls.__name__} has no real or computed field named {field_name!r}")
                return cls.get_column_metadata(info)
            case FieldInfo(metadata=metadata):
                return only((m._copy() for m in metadata if isinstance(m, Column)), default=None)
            case ComputedFieldInfo(return_type=None):
                return None
            case ComputedFieldInfo(return_type=annotation) if is_pep593(annotation):
                args = get_args(annotation)
                return only((m._copy() for m in args if isinstance(m, Column)), default=None)
            case ComputedFieldInfo(return_type=annotation):
                return None
            case _:
                raise TypeError(f"Expected FieldInfo, ComputedFieldInfo, or str; got {type(field)}")

    @classmethod
    def get_relationship_table_def(cls, field: FieldInfo | ComputedFieldInfo | str) -> RelationshipTableDef | None:
        """
        Retrieves a copy of the `RelationshipTableDef` explicitly defined on a model field's `Annotated` metadata, if any.
        :param field: A `FieldInfo`, `ComputedFieldInfo`, or field name.
        :return: The `RelationshipTableDef` defined on the field, or `None` if there isn't one.
        :raises KeyError: if the field name does not exist on this model.
        :raises TypeError: if the field is not one of the expected types.
        """
        match field:
            case str(field_name):
                info = cls.model_fields.get(field_name) or cls.model_computed_fields.get(field_name)
                if info is None:
                    raise KeyError(f"Model {cls.__name__} has no real or computed field named {field_name!r}")
                return cls.get_relationship_table_def(info)
            case FieldInfo(metadata=metadata):
                return only((m for m in metadata if isinstance(m, RelationshipTableDef)), default=None)
            case ComputedFieldInfo(return_type=None):
                return None
            case ComputedFieldInfo(return_type=annotation) if is_pep593(annotation):
                args = get_args(annotation)
                return only((m for m in args if isinstance(m, RelationshipTableDef)), default=None)
            case ComputedFieldInfo(return_type=annotation):
                return None
            case _:
                raise TypeError(f"Expected FieldInfo or ComputedFieldInfo, got {type(field)}")

    @classmethod
    @cache
    def pk_columns(cls) -> immutabledict[str, Column]:
        """
        Returns a dictionary of primary key columns for the model.

        :return: A dictionary mapping field names to Column instances that are primary keys.
        """

        return immutabledict({k:v for k, v in cls.columns().items() if v.primary_key})

    @classmethod
    @cache
    def columns(cls) -> immutabledict[str, Column]:
        """
        Gets all Column instances from this model type's fields
        as defined in their Annotated metadata.
        Absent values will be filled in with defaults.

        Returns a map of field names to Column instances,
        empty if none are found.

        :returns: A map of field names to `Column` instances,
        where each `Column` is populated by the field's explicit annotation
        with additional defaults.
        """

        result: dict[str, Column] = {}

        all_fields = chain(cls.model_fields.items(), cls.model_computed_fields.items())
        for field_name, field in all_fields:
            # For each field (real or computed)...
            if isinstance(field, FieldInfo) and field.exclude:
                # Don't create Columns for excluded fields
                continue

            if cls.get_relationship_table_def(field):
                # Don't create Columns for known relationship tables
                continue

            field_annotation = cls.get_field_annotation(field)
            unwrapped_field_type = cls.unwrap_type(field_annotation)
            field_origin = get_origin(field_annotation)
            if is_non_string_sequence_type(field_origin):
                # Skip automatic FK generation for collection types,
                # otherwise we run the risk of infinite recursion
                # (they should use RelationshipTableDef instead)
                continue

            if (column := cls.get_column_metadata(field)) is None:
                # If this field doesn't define a Column, create a new one with defaults
                column = Column()
            else:
                assert column.table is None, f"{column} unexpectedly linked to {column.table}, did something mutate it?"
                # Columns have internal state,
                # so we shouldn't mutate the ones in the Annotated metadata
                column = column._copy()

            if not column.name:
                # Use the field name as the column name if not given
                column.name = field_name

            if column._user_defined_nullable == SchemaConst.NULL_UNSPECIFIED:
                # If nullability isn't specified, infer it from the field annotation
                column.nullable = includes_none(field_annotation)

            field_type = cls.get_field_type(field)
            if issubclass(field_type, DatabaseModel):
                # If this field refers to another model...
                if field_type != cls and not column.foreign_keys:
                    # If this column doesn't already define any foreign keys,
                    # get the default foreign keys that the target type uses
                    for fk in field_type.get_default_foreign_keys():
                        assert fk.parent is None, f"Default ForeignKey to {field_type.__name__} has an unexpected parent {fk.parent}; did something mutate it?"
                        column.append_foreign_key(fk._copy())

                # Otherwise, this column already has foreign keys, so use them;
                # the type doesn't matter, SQLAlchemy will infer it when building the Tables
            elif isinstance(column.type, (NullType, type(None))):
                # If there isn't a type already, infer it from the field annotation
                column.type = cls.get_default_column_type(field)()

            result[field_name] = column

        return immutabledict(result)

    @classmethod
    @cache
    def relationship_table_defs(cls) -> immutabledict[str, RelationshipTableDef]:
        """
        Gets all RelationshipTableDef instances from this model type's fields,
        as defined in their Annotated metadata.
        Absent values will be filled in with defaults.

        Returns a map of field names to RelationshipTableDef instances,
        empty if none are found.

        :raises ValueError: if multiple RelationshipTableDefs are found on a single field.
        """

        result: dict[str, RelationshipTableDef] = {}
        all_fields = chain(cls.model_fields.items(), cls.model_computed_fields.items())
        for field_name, field in all_fields:
            if isinstance(field, FieldInfo) and field.exclude:
                # Don't create relationship tables that represent excluded fields
                continue

            field_annotation = cls.get_field_annotation(field)
            field_origin = get_origin(field_annotation)
            field_type = cls.get_field_type(field)

            defn = cls.get_relationship_table_def(field)
            if not is_non_string_sequence_type(field_origin):
                # Relationship tables only make sense for collection types;
                # raise an error if one is inappropriately defined, otherwise just move on
                if not defn:
                    # No RelationshipTableDef is explicitly defined, and there shouldn't be one; good!
                    continue
                else:
                    raise TypeError(f"Cannot create relationship table for non-collection field {cls.__name__}.{field_name} of type {field_type}")

            defn = deepcopy(defn) if defn else RelationshipTableDef()
            # If no RelationshipTableDef is defined, create a default one;
            # otherwise create a deep copy of the existing one to avoid mutating it
            # (since SQLAlchemy Table/Column/etc. instances have internal state)

            if not defn.tablename:
                # If the RelationshipTableDef doesn't specify a table name,
                # generate a default one based on the parent table and field name
                defn.tablename = f"{cls.__tablename__}_{field_name}"

            if not defn.self_columns:
                # If the RelationshipTableDef doesn't specify columns that reference this object,
                # generate defaults with this class's primary key columns
                pk_cols = cls.pk_columns()
                if not pk_cols:
                    raise ValueError(
                        f"Cannot create default relationship table for {cls.__name__}.{field_name!r} "
                        f"because this model has no primary key columns; "
                        "try defining one explicitly"
                    )

                defn.self_columns = tuple(
                    Column(
                        f"{defn.tablename}_{pkcol.name}",
                        ForeignKey(f"{cls.__tablename__}.{pkcol.name}"),
                        primary_key=True
                    )
                    for pkcol in pk_cols.values()
                )

            if not defn.related_columns:
                # If the RelationshipTableDef doesn't specify columns that reference the related object,
                # generate defaults based on the related type
                field_type_args = get_args(field_annotation)
                related_type = cls.unwrap_type(field_type_args[0])
                # TODO: Is this the right way to get the related type?

                if issubclass(related_type, DatabaseModel):
                    related_pk_cols = related_type.pk_columns()
                    if not related_pk_cols:
                        raise ValueError(
                            f"Cannot create default relationship table for {cls.__name__}.{field_name!r} "
                            f"because related model {related_type.__name__} has no primary key columns; "
                            "try defining one explicitly"
                        )

                    defn.related_columns = tuple(
                        Column(
                            f"{related_type.__tablename__}_{pkcol.name}",
                            ForeignKey(f"{related_type.__tablename__}.{pkcol.name}"),
                            primary_key=True
                        )
                        for pkcol in related_pk_cols.values()
                    )
                else:
                    # Primitive type
                    defn.related_columns = (
                        Column(
                            f"{related_type.__tablename__}_{field_name}",
                            cls.get_default_column_type(related_type),
                            primary_key=True
                        ),
                    )


            result[field_name] = defn

        return immutabledict(result)

    @classmethod
    def create_tables(cls, metadata: MetaData) -> tuple[Table, *tuple[Table, ...]]:
        """
        Creates a SQLAlchemy Table object for this model type,
        and any associated relationship tables.

        :param metadata: The `MetaData` to associate the tables with.
        :return: A tuple of `Table`s, where the first item is the main table
                    and any subsequent items are relationship tables.
        """
        main_table = Table(cls.__tablename__, metadata)
        relationship_tables: list[Table] = []

        columns = cls.columns()
        relationship_table_defs = cls.relationship_table_defs()
        all_fields = chain(cls.model_fields.items(), cls.model_computed_fields.items())
        for field_name, field in all_fields:
            match field:
                case FieldInfo(exclude=True):
                    # Excluded fields won't have columns
                    pass
                case (FieldInfo() | ComputedFieldInfo()) if field_name in columns:
                    # Columns contain internal state, so we need to copy them;
                    # otherwise SQLAlchemy will think we're adding the same Column to multiple tables
                    main_table.append_column(columns[field_name]._copy())
                case (FieldInfo() | ComputedFieldInfo()) if field_name in relationship_table_defs:
                    reldef = relationship_table_defs[field_name]
                    if __debug__:
                        for col in reldef.self_columns:
                            assert col.table is None, f"{col} unexpectedly linked to {col.table}, did something mutate it?"

                        for col in reldef.related_columns:
                            assert col.table is None, f"{col} unexpectedly linked to {col.table}, did something mutate it?"

                    reltable = Table(
                        reldef.tablename or f"{cls.__tablename__}_{field_name}",
                        metadata,
                        *(c._copy() for c in reldef.self_columns),
                        *(c._copy() for c in reldef.related_columns),
                        *(i._copy() for i in reldef.tableargs),
                        **reldef.tablekwargs,
                    )
                    relationship_tables.append(reltable)

        return (main_table, *relationship_tables)

    @cached_property
    def nested_models(self) -> frozenset["DatabaseModel"]:
        """
        Returns a set of all nested DatabaseModel instances referenced by this model's fields,
        excluding itself.

        Checks immediate attributes,
        but only recurses into attributes that are also DatabaseModel instances or lists of them.
        You can subclass this behavior if you need more complex recursion.
        """
        models: set[DatabaseModel] = set()
        for field_name in chain(type(self).model_fields, type(self).model_computed_fields):
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
        reldefs = cls.relationship_table_defs()

        for field_name in chain(cls.model_fields, cls.model_computed_fields):
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

    @property
    def as_row(self) -> dict[str, Any]:
        return self.model_dump(context='row')


type CoercedHttpUrl = Annotated[HttpUrl, WrapValidator(lambda v, h: h(v) if v else None), PlainSerializer(str, str)]
type InsertInRowContext = Literal['row'] | None

def validate_frozendict(v: Any, handler: ValidatorFunctionWrapHandler) -> immutabledict[Any, Any]:
    if isinstance(v, immutabledict):
        return v

    if isinstance(v, Mapping):
        return immutabledict(handler(v))

    raise TypeError(f"Expected immutabledict or Mapping, got {type(v)}")

FrozenDictValidator = WrapValidator(validate_frozendict)

type FrozenDict[K, V] = Annotated[immutabledict[K, V], FrozenDictValidator]
Hash = Annotated[str, StringConstraints(to_lower=True)]
type WrapInTuple[T] = Annotated[tuple[T, ...], BeforeValidator(lambda v: always_iterable(v))]

type EmptyStringToNone[T] = Annotated[
    T | None,
    BeforeValidator(lambda v: v if v != "" else None),
    WrapSerializer(lambda v, h: h(v) if v != "" else None, return_type=(T | None))
]
"""
A type that serializes and validates empty strings as None.
"""

__all__ = (
    "DatabaseModel",
    "RelationshipTableDef",
    "Hash",
    "is_non_string_sequence_type",
    "CoercedHttpUrl",
    "FrozenDictValidator",
    "InsertInRowContext",
    "WrapInTuple",
    "FrozenDict",
)