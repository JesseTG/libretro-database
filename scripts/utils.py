#!/usr/bin/env python3

"""
Provides base classes and utilities for defining database models using Pydantic and SQLAlchemy.
"""

import sys

from abc import ABC
from collections.abc import Iterable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from datetime import date, datetime
from functools import cache, cached_property
from itertools import chain
from typing import Annotated, Any, ClassVar, ForwardRef, Literal, NewType, Self, TypeGuard, get_origin

import sqlalchemy

from frozendict import frozendict
from more_itertools import always_iterable, only
from pydantic import AfterValidator, BaseModel, BeforeValidator, GetCoreSchemaHandler, GetPydanticSchema, HttpUrl, JsonValue, PlainSerializer, StringConstraints, ValidatorFunctionWrapHandler, WrapSerializer, WrapValidator
from pydantic_core import CoreSchema, core_schema
from pydantic.fields import ComputedFieldInfo, FieldInfo
from pydantic_extra_types.country import CountryNumericCode
from sqlalchemy import DDL, Column, ForeignKey, MetaData, Table
from sqlalchemy import event
from sqlalchemy.schema import SchemaConst, SchemaItem
from sqlalchemy.types import NullType, TypeEngine
from sqlalchemy.util.typing import (GenericProtocol, TypeAliasType,
                                    de_optionalize_union_types,
                                    eval_expression, flatten_newtype, get_args,
                                    includes_none, is_fwd_ref, is_generic,
                                    is_literal, is_newtype, is_pep593, is_pep695, is_union, make_union_type)

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

type RelationshipTableArg = Mapping[str, Column] | Iterable[Column] | Column | ForeignKey

EMPTY_DICT = frozendict()

@dataclass(eq=True, unsafe_hash=True)
class Relationship:

    tablename: str | None
    """
    The name of the relationship table that will be created
    to represent this relationship.

    If None, a default name will be generated based on the parent model's table name
    and the Pydantic field name that this Relationship is associated with.
    """

    self_columns: frozendict[str, Column]
    """
    One or more `Column`s that identify the "parent" object.
    Each key is a field name on the parent object's model,
    and each value is a corresponding column in the generated relationship table.
    """

    related_columns: frozendict[str, Column]
    """
    One or more `Column`s that define the related object.

    Can be references to another `DatabaseModel`'s primary key columns,
    or primitive values.
    """

    tableargs: tuple[CopyableSchemaItem, ...]
    """
    Positional arguments to pass as-is to the Table constructor
    after the table name, metadata, and explicit constraints.
    Useful for table-level constraints.
    """

    tablekwargs: frozendict[str, Any]
    """
    Keyword arguments to pass as-is to the Table constructor.
    """

    def __init__(
        self,
        tablename: str | None = None,
        self_columns: RelationshipTableArg = EMPTY_DICT,
        related_columns: RelationshipTableArg = EMPTY_DICT,
        tableargs: tuple[CopyableSchemaItem, ...] = (),
        tablekwargs: frozendict[str, Any] | None = None
    ):
        self.tablename = tablename

        match self_columns:
            case Column() as column:
                self.self_columns = frozendict({column.name: column})
            case ForeignKey() as fk:
                name = fk.target_fullname.split(".")[-1]
                column_name = fk.target_fullname.replace(".", "_")
                self.self_columns = frozendict({name: Column(column_name, fk._copy(), nullable=False)})
            case { **items }:
                self.self_columns = frozendict(items)
            case [*columns]:
                self.self_columns = frozendict({c.name: c for c in columns})
            case _:
                raise TypeError(f"Unsupported self_columns type: {type(self_columns)}")

        match related_columns:
            case Column() as column:
                self.related_columns = frozendict({column.name: column})
            case ForeignKey() as fk:
                name = fk.target_fullname.split(".")[-1]
                column_name = fk.target_fullname.replace(".", "_")
                self.related_columns = frozendict({name: Column(column_name, fk._copy(), nullable=False)})
            case { **items }:
                self.related_columns = frozendict(items)
            case [*columns]:
                self.related_columns = frozendict({c.name: c for c in columns})
            case _:
                raise TypeError(f"Unsupported related_columns type: {type(related_columns)}")

        self.tableargs = tableargs
        self.tablekwargs = tablekwargs or EMPTY_DICT


    def __deepcopy__(self, memo: dict[int, Any]) -> "Relationship":
        return Relationship(
            tablename=self.tablename,
            self_columns=frozendict({k: v._copy() for k, v in self.self_columns.items()}),
            related_columns=frozendict({k: c._copy() for k, c in self.related_columns.items()}),
            tableargs=tuple(i._copy() for i in self.tableargs),
            tablekwargs=frozendict(self.tablekwargs),
        )

SchemaDef = Column | Relationship

class DatabaseModel(BaseModel, ABC, frozen=True):
    __tablename__: ClassVar[str]
    __tableconstraints__: ClassVar[tuple[SchemaItem, ...]] = ()
    __tablekwargs__: ClassVar[Mapping[str, Any]] = EMPTY_DICT
    __tableddl__: ClassVar[str | None] = None
    """
    Extra DDL statements to execute after creating this class's table.
    Intended for database-specific features that SQLAlchemy doesn't natively support.
    """

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
        unwrapped = t
        while not isinstance(unwrapped, type):
            match unwrapped:
                case type():
                    break
                case newtype if is_newtype(newtype):
                    # If this is a newtype, unwrap to get the underlying type
                    unwrapped = flatten_newtype(newtype)
                case literal if is_literal(literal):
                    # If this is a Literal[A, B, C, ...], unwrap to get the types of A, B, C, ...
                    # (but if there's just one unique type, resolve to it)
                    args = get_args(literal)
                    literal_types = set(map(type, args))
                    unwrapped = type(args[0]) if len(literal_types) == 1 else make_union_type(*literal_types)
                case annotation if is_pep593(annotation):
                    # If this is Annotated[T, ...], unwrap to get T
                    args = get_args(annotation)
                    assert len(args) >= 2
                    unwrapped = args[0]
                case ref if is_fwd_ref(ref, check_generic=True, check_for_plain_string=True):
                    # If this is a ForwardRef...
                    unwrapped = eval_expression(ref.__forward_arg__, cls.__module__, locals_=sys.modules[cls.__module__].__dict__)
                case str() as type_expression:
                    unwrapped = eval_expression(type_expression, cls.__module__, locals_=sys.modules[cls.__module__].__dict__)
                case alias if is_pep695(alias) and not alias.__type_params__:
                    # If this is a type alias without parameters...
                    unwrapped = alias.__value__
                case alias if is_pep695(alias) and (args := get_args(alias)):
                    # If this is a parameterized type alias...
                    unwrapped = alias.__value__[args]
                case generic if is_generic(generic) and not is_union(generic): # and is_non_string_sequence_type(get_origin(generic)):
                    # If this is parameterized type like list[T]...
                    unwrapped = get_origin(generic)
                    assert unwrapped is not None
                case optional if includes_none(unwrapped):
                    # If this type can have a value of None...
                    # (For most purposes you can treat it as Optional[T],
                    # but Python has several equivalent constructs.)
                    unwrapped = de_optionalize_union_types(unwrapped)
                case _:
                    raise TypeError(f"Unexpected type annotation: {t} ({type(t)})")

        assert isinstance(unwrapped, type)
        return unwrapped

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
    def get_relationship_table_def(cls, field: FieldInfo | ComputedFieldInfo | str) -> Relationship | None:
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
                return only((m for m in metadata if isinstance(m, Relationship)), default=None)
            case ComputedFieldInfo(return_type=None):
                return None
            case ComputedFieldInfo(return_type=annotation) if is_pep593(annotation):
                args = get_args(annotation)
                return only((m for m in args if isinstance(m, Relationship)), default=None)
            case ComputedFieldInfo(return_type=annotation):
                return None
            case _:
                raise TypeError(f"Expected FieldInfo or ComputedFieldInfo, got {type(field)}")

    @classmethod
    @cache
    def pk_columns(cls) -> frozendict[str, Column]:
        """
        Returns a dictionary of primary key columns for the model.

        :return: A dictionary mapping field names to Column instances that are primary keys.
        """

        return frozendict({k:v for k, v in cls.columns().items() if v.primary_key})

    @classmethod
    @cache
    def columns(cls) -> frozendict[str, Column]:
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
            if is_non_string_sequence_type(field_origin) or is_non_string_sequence_type(unwrapped_field_type):
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
                # TODO: Raise a warning if _user_defined_nullable doesn't exist,
                # as it means SQLAlchemy's internals have changed
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

        return frozendict(result)

    @classmethod
    @cache
    def relationship_table_defs(cls) -> frozendict[str, Relationship]:
        """
        Gets all RelationshipTableDef instances from this model type's fields,
        as defined in their Annotated metadata.
        Absent values will be filled in with defaults.

        :returns: A dict of field names to RelationshipTableDef instances.
        The key will always be a field in this class,
        regardless of what the table or its columns are named.

        :raises ValueError: if multiple RelationshipTableDefs are found on a single field.
        """

        result: dict[str, Relationship] = {}
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

            defn = deepcopy(defn) if defn else Relationship()
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
                        f"Cannot create default relationship table for {cls.__name__}.{field_name} "
                        f"because {cls.__name__}'s generated table has no primary key columns; "
                        "try defining one explicitly by passing a Column to one of its Annotated fields "
                        "and setting primary_key=True"
                    )


                # By default, generate a column for each component of this class's primary key
                # and make it part of the relationship table row's composite primary key.
                # (Whew! What a mouthful.)
                defn.self_columns = frozendict({
                    pkcol_name: Column(
                        f"{defn.tablename}_{pkcol.name}",
                        ForeignKey(f"{cls.__tablename__}.{pkcol.name}"),
                        primary_key=True
                    ) for pkcol_name, pkcol in pk_cols.items()
                })

            if not defn.related_columns:
                # If the RelationshipTableDef doesn't specify columns that reference the related object,
                # generate defaults based on the related type
                field_type_args = get_args(field_annotation)
                related_type = cls.unwrap_type(field_type_args[0])
                # TODO: Is this the right way to get the related type?

                if issubclass(related_type, DatabaseModel):
                    # If this field is a collection of other database models...
                    related_pk_cols = related_type.pk_columns()
                    if not related_pk_cols:
                        raise ValueError(
                            f"Cannot create default relationship table for {cls.__name__}.{field_name} "
                            f"because related model {related_type.__name__} has no primary key columns; "
                            "try defining one explicitly"
                        )

                    # ...add a column for each part of the related type's primary key
                    defn.related_columns = frozendict({
                        pkcol_name: Column(
                            f"{related_type.__tablename__}_{pkcol.name}",
                            ForeignKey(f"{related_type.__tablename__}.{pkcol.name}"),
                            primary_key=True
                        )
                        for pkcol_name, pkcol in related_pk_cols.items()
                    })
                else:
                    # This field is a collection of primitive values
                    defn.related_columns = frozendict({
                        field_name: Column(
                            f"{cls.__tablename__}_{field_name}",
                            cls.get_default_column_type(related_type),
                            primary_key=True
                        ),
                    })

            result[field_name] = defn

        return frozendict(result)

    @classmethod
    def create_tables(cls, metadata: MetaData) -> tuple[Table, *tuple[Table, ...]]:
        """
        Creates a SQLAlchemy Table object for this model type,
        and any associated relationship tables.

        :param metadata: The `MetaData` to associate the tables with.
        :return: A tuple of `Table`s, where the first item is the main table
                    and any subsequent items are relationship tables.
        """
        main_table = Table(
            cls.__tablename__,
            metadata,
            *cls.__tableconstraints__,
            **cls.__tablekwargs__,
        )
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
                        for colname, col in reldef.self_columns.items():
                            assert col.table is None, f"{col} representing {colname} unexpectedly linked to {col.table}, did something mutate it?"

                        for colname, col in reldef.related_columns.items():
                            assert col.table is None, f"{col} representing {colname} unexpectedly linked to {col.table}, did something mutate it?"

                    reltable = Table(
                        reldef.tablename or f"{cls.__tablename__}_{field_name}",
                        metadata,
                        *(c._copy() for c in reldef.self_columns.values()),
                        *(c._copy() for c in reldef.related_columns.values()),
                        *(i._copy() for i in reldef.tableargs),
                        **reldef.tablekwargs,
                    )
                    relationship_tables.append(reltable)

        if cls.__tableddl__:
            # If we want to execute any extra data definition language statements (e.g. CREATE, ALTER, etc.),
            # register an event listener to do so after creating the table
            event.listen(main_table, "after_create", DDL(cls.__tableddl__))

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
                case obj if isinstance(obj, DatabaseModel):
                    models.add(obj)
                    models.update(obj.nested_models)
                case [*items]:
                    objects = (i for i in items if isinstance(i, DatabaseModel))
                    for obj in objects:
                        models.add(obj)
                        models.update(obj.nested_models)

        return frozenset(models)

    def get_relationship(self, field_name: str) -> tuple[frozendict[str, Any], ...]:
        cls = type(self)
        reldefs = cls.relationship_table_defs()
        reldef = reldefs.get(field_name)

        if not reldef:
            return ()

        field = cls.model_fields.get(field_name) or cls.model_computed_fields.get(field_name)
        if not field:
            return ()

        field_annotation = cls.get_field_annotation(field)
        field_origin = get_origin(field_annotation)
        if not is_non_string_sequence_type(field_origin):
            # If the field isn't a non-string sequence type, we don't handle it here
            return ()

        rows: list[frozendict[str, Any]] = []
        field_value = getattr(self, field_name)
        for v in field_value:
            # For each item in this collection...
            row: dict[str, Any] = {}
            for col_fieldname, col in reldef.self_columns.items():
                # For each column that's used to identify this object...
                row[col.name] = getattr(self, col_fieldname)
            for col_fieldname, col in reldef.related_columns.items():
                # For each column that's used to identify the related object...
                row[col.name] = getattr(v, col_fieldname) if isinstance(v, DatabaseModel) else v
                # ...set it to the field value if it's another DatabaseModel,
                # otherwise just use the value directly
            rows.append(frozendict(row))

        return tuple(rows)

    @cached_property
    def relationships(self) -> frozendict[str, tuple[frozendict[str, Any], ...]]:
        """
        Returns a dictionary whose keys are field names representing relationships,
        and whose values are sets of dicts suitable for relationship tables.
        These dicts include the foreign key mappings for this object and the related objects.

        This property is not recursive, i.e. it does not include relationships from nested models.
        """
        results: dict[str, tuple[frozendict[str, Any], ...]] = {}
        cls = type(self)

        for field_name in chain(cls.model_fields, cls.model_computed_fields):
            rows = self.get_relationship(field_name)
            if rows:
                results[field_name] = tuple(rows)

        return frozendict(results)

    @property
    def as_row(self) -> dict[str, Any]:
        return self.model_dump(context='row')


type CoercedHttpUrl = Annotated[HttpUrl, WrapValidator(lambda v, h: h(v) if v else None), PlainSerializer(str, str)]
type InsertInRowContext = Literal['row'] | None

def validate_frozendict(v: Any, handler: ValidatorFunctionWrapHandler) -> frozendict[Any, Any]:
    if isinstance(v, frozendict):
        return v

    if isinstance(v, Mapping):
        return frozendict(handler(v))

    raise TypeError(f"Expected frozendict or Mapping, got {type(v)}")

def _frozen_dict_schema(tp: Any, handler: GetCoreSchemaHandler) -> CoreSchema:
    args = get_args(tp)
    key_type = args[0] if len(args) >= 1 else Any
    value_type = args[1] if len(args) >= 2 else Any

    return core_schema.no_info_after_validator_function(
        frozendict,
        core_schema.dict_schema(
            keys_schema=handler.generate_schema(key_type),
            values_schema=handler.generate_schema(value_type)
        )
    )

def ZeroPad(min_length: int):
    return BeforeValidator(lambda s: s.zfill(min_length))

type FrozenDict[K, V] = Annotated[frozendict[K, V], GetPydanticSchema(_frozen_dict_schema)]
type TypedFrozenDict[T] = Annotated[T, AfterValidator(lambda v: frozendict(v))]
# NOTE: The pattern is in Rust syntax, not Python syntax! (Pydantic-core is implemented in Rust.)
Crc = Annotated[str, ZeroPad(8), StringConstraints(to_lower=True, pattern=r"[a-fA-F0-9]{8}")]
Md5 = Annotated[str, StringConstraints(to_lower=True, pattern=r"[a-fA-F0-9]{32}")]
Sha1 = Annotated[str, StringConstraints(to_lower=True, pattern=r"[a-fA-F0-9]{40}")]
Sha256 = Annotated[str, StringConstraints(to_lower=True, pattern=r"[a-fA-F0-9]{64}")]


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
    "Relationship",
    "Sha256",
    "Sha1",
    "Md5",
    "Crc",
    "EmptyStringToNone",
    "is_non_string_sequence_type",
    "CoercedHttpUrl",
    "InsertInRowContext",
    "WrapInTuple",
    "FrozenDict",
)