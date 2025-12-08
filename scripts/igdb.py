#!/usr/bin/env python3

import argparse
import asyncio
import dataclasses
import itertools
import os.path
import re
import sys
import tomllib

from asyncio import TaskGroup
from collections import ChainMap
from collections.abc import Collection, Sequence, Iterable, Iterator, Mapping, Set
from concurrent.futures import Executor
from dataclasses import dataclass, Field
from functools import cache
from json import JSONDecodeError
from pathlib import Path
from typing import ClassVar, Never, Optional, Literal, NewType, Protocol, Required, Self, TypeAlias, TypeVar, TypedDict, cast, overload, TYPE_CHECKING, override, runtime_checkable

import aiofiles
import aiofiles.os
import asynciolimiter
import backoff
import httpx
import orjson
import typelib

from authlib.integrations.httpx_client import AsyncOAuth2Client
from authlib.oauth2.rfc6749 import OAuth2Token
from httpx import HTTPStatusError, Response, Timeout
from pydantic import TypeAdapter, JsonValue, ValidationError
from pydantic_core import from_json, to_json

IgdbId = NewType('IgdbId', int)
PlaylistTitle = NewType('PlaylistTitle', str)
IgdbIndexRow = Mapping[str, int | bool | float | str | None]
IgdbIndexRelationships = Mapping[str, Set[tuple[IgdbId, IgdbId]]]

if TYPE_CHECKING:
    from _typeshed import DataclassInstance
else:
    class DataclassInstance(Protocol):
        # The real thing has a __dataclass_fields__ member,
        # but we don't need it here.
        # Adding it makes typelib raise a warning anyway,
        # since its definition includes `Any`.
        pass


D = TypeVar('D', bound=DataclassInstance, covariant=True)

@runtime_checkable
class IgdbObject(DataclassInstance, Protocol[D]):
    id: IgdbId

    def to_row(self) -> IgdbIndexRow:
        return dataclasses.asdict(self)

    def to_relationships(self) -> IgdbIndexRelationships:
        return {}

    __table__: ClassVar[str]


@dataclasses.dataclass(frozen=True, kw_only=True, slots=True)
class AgeRatingOrganization(IgdbObject):
    id: IgdbId
    name: str
    __table__: ClassVar[str] = """
        CREATE TABLE IF NOT EXISTS IgdbAgeRatingOrganization (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL COLLATE RTRIM
        );
    """

@dataclasses.dataclass(frozen=True, kw_only=True, slots=True)
class AgeRatingCategory(IgdbObject):
    id: IgdbId
    rating: str
    __table__: ClassVar[str] = """
        CREATE TABLE IF NOT EXISTS IgdbAgeRatingCategory (
            id INTEGER PRIMARY KEY,
            rating TEXT NOT NULL COLLATE RTRIM
        );
    """

@dataclasses.dataclass(frozen=True, kw_only=True, slots=True)
class AgeRatingContentDescriptionType(IgdbObject):
    id: IgdbId
    name: str
    __table__: ClassVar[str] = """
        CREATE TABLE IF NOT EXISTS IgdbAgeRatingContentDescriptionType (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL
        );
    """

@dataclasses.dataclass(frozen=True, kw_only=True, slots=True)
class AgeRatingContentDescriptionV2(IgdbObject):
    id: IgdbId
    description: str
    description_type: AgeRatingContentDescriptionType
    __table__: ClassVar[str] = """
        CREATE TABLE IF NOT EXISTS IgdbAgeRatingContentDescriptionV2 (
            id INTEGER PRIMARY KEY,
            description TEXT NOT NULL,
            description_type INTEGER NOT NULL REFERENCES IgdbAgeRatingContentDescriptionType(id)
        );
    """

    @override
    def to_row(self) -> IgdbIndexRow:
        return {
            'id': self.id,
            'description': self.description,
            'description_type': self.description_type.id,
        }

@dataclasses.dataclass(frozen=True, kw_only=True, slots=True)
class AgeRating(IgdbObject):
    id: IgdbId
    organization: AgeRatingOrganization
    rating_category: AgeRatingCategory
    rating_content_descriptions: Optional[Sequence[AgeRatingContentDescriptionV2]] = None
    rating_cover_url: Optional[str] = None
    synopsis: Optional[str] = None
    __table__: ClassVar[str] = """
        CREATE TABLE IF NOT EXISTS IgdbAgeRating (
            id INTEGER PRIMARY KEY,
            organization INTEGER NOT NULL REFERENCES IgdbAgeRatingOrganization(id),
            rating_category INTEGER NOT NULL REFERENCES IgdbAgeRatingCategory(id),
            rating_cover_url TEXT,
            synopsis TEXT
        );
        CREATE TABLE IF NOT EXISTS IgdbAgeRating_rating_content_descriptions (
            age_rating INTEGER NOT NULL REFERENCES IgdbAgeRating(id),
            rating_content_description INTEGER NOT NULL REFERENCES IgdbAgeRatingContentDescriptionV2(id),

            PRIMARY KEY (age_rating, rating_content_description)
        );
    """

    def __post_init__(self) -> None:
        if self.rating_content_descriptions is not None and not isinstance(self.rating_content_descriptions, tuple):
            object.__setattr__(self, 'rating_content_descriptions', tuple(self.rating_content_descriptions))

    @override
    def to_row(self) -> IgdbIndexRow:
        return {
            'id': self.id,
            'organization': self.organization.id,
            'rating_category': self.rating_category.id,
            'rating_cover_url': self.rating_cover_url,
            'synopsis': self.synopsis,
        }

    @override
    def to_relationships(self) -> IgdbIndexRelationships:
        if not self.rating_content_descriptions:
            return {}

        return {
            "IgdbAgeRating_rating_content_descriptions": {
                (self.id, description.id) for description in self.rating_content_descriptions
            }
        }

@dataclasses.dataclass(frozen=True, kw_only=True, slots=True)
class AlternativeName(IgdbObject):
    id: IgdbId
    name: str
    comment: Optional[str] = None

    __table__: ClassVar[str] = """
        CREATE TABLE IF NOT EXISTS IgdbAlternativeName (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            comment TEXT
        );
    """

@dataclasses.dataclass(frozen=True, kw_only=True, slots=True)
class Franchise(IgdbObject):
    id: IgdbId
    name: str
    slug: str

    __table__: ClassVar[str] = """
        CREATE TABLE IF NOT EXISTS IgdbFranchise (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            slug TEXT NOT NULL COLLATE RTRIM
        );
    """

@dataclasses.dataclass(frozen=True, kw_only=True, slots=True)
class GameEngine(IgdbObject):
    id: IgdbId
    name: str
    slug: str

    __table__: ClassVar[str] = """
        CREATE TABLE IF NOT EXISTS IgdbGameEngine (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            slug TEXT NOT NULL COLLATE RTRIM
        );
    """

@dataclasses.dataclass(frozen=True, kw_only=True, slots=True)
class GameLocalization(IgdbObject):
    id: IgdbId
    name: Optional[str] = None
    region: 'Region'

    __table__: ClassVar[str] = """
        CREATE TABLE IF NOT EXISTS IgdbGameLocalization (
            id INTEGER PRIMARY KEY,
            name TEXT,
            region INTEGER NOT NULL REFERENCES IgdbRegion(id)
        );
    """

    @override
    def to_row(self) -> IgdbIndexRow:
        return {
            'id': self.id,
            'name': self.name,
            'region': self.region.id,
        }

@dataclasses.dataclass(frozen=True, kw_only=True, slots=True)
class GameMode(IgdbObject):
    id: IgdbId
    name: str

    __table__: ClassVar[str] = """
        CREATE TABLE IF NOT EXISTS IgdbGameMode (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL
        );
    """

@dataclasses.dataclass(frozen=True, kw_only=True, slots=True)
class GameStatus(IgdbObject):
    id: IgdbId
    status: str

    __table__: ClassVar[str] = """
        CREATE TABLE IF NOT EXISTS IgdbGameStatus (
            id INTEGER PRIMARY KEY,
            status TEXT NOT NULL
        );
    """

@dataclasses.dataclass(frozen=True, kw_only=True, slots=True)
class GameType(IgdbObject):
    id: IgdbId
    type: str

    __table__: ClassVar[str] = """
        CREATE TABLE IF NOT EXISTS IgdbGameType (
            id INTEGER PRIMARY KEY,
            type TEXT NOT NULL
        );
    """

@dataclasses.dataclass(frozen=True, kw_only=True, slots=True)
class Genre(IgdbObject):
    id: IgdbId
    name: str

    __table__: ClassVar[str] = """
        CREATE TABLE IF NOT EXISTS IgdbGenre (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL
        );
    """

@dataclasses.dataclass(frozen=True, kw_only=True, slots=True)
class CompanyStatus(IgdbObject):
    id: IgdbId
    name: str

    __table__: ClassVar[str] = """
        CREATE TABLE IF NOT EXISTS IgdbCompanyStatus (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL
        );
    """

@dataclasses.dataclass(frozen=True, kw_only=True, slots=True)
class Company(IgdbObject):
    id: IgdbId
    country: Optional[int] = None # ISO 3166-1 code
    name: str
    slug: str
    status: Optional[CompanyStatus] = None

    __table__: ClassVar[str] = """
        CREATE TABLE IF NOT EXISTS IgdbCompany (
            id INTEGER PRIMARY KEY,
            country INTEGER, -- ISO 3166-1 code
            name TEXT NOT NULL,
            slug TEXT NOT NULL COLLATE RTRIM,
            status INTEGER REFERENCES IgdbCompanyStatus(id)
        );
    """

    @override
    def to_row(self) -> IgdbIndexRow:
        return {
            'id': self.id,
            'country': self.country,
            'name': self.name,
            'slug': self.slug,
            'status': self.status.id if self.status else None,
        }

@dataclasses.dataclass(frozen=True, kw_only=True, slots=True)
class InvolvedCompany(IgdbObject):
    id: IgdbId
    company: Company
    developer: bool
    porting: bool
    publisher: bool
    supporting: bool

    __table__: ClassVar[str] = """
        CREATE TABLE IF NOT EXISTS IgdbInvolvedCompany (
            id INTEGER PRIMARY KEY,
            company INTEGER NOT NULL REFERENCES IgdbCompany(id),
            developer BOOLEAN,
            porting BOOLEAN,
            publisher BOOLEAN,
            supporting BOOLEAN
        );
    """

    @override
    def to_row(self) -> IgdbIndexRow:
        return {
            'id': self.id,
            'company': self.company.id,
            'developer': self.developer,
            'porting': self.porting,
            'publisher': self.publisher,
            'supporting': self.supporting,
        }

@dataclasses.dataclass(frozen=True, kw_only=True, slots=True)
class Region(IgdbObject):
    id: IgdbId
    identifier: Optional[str] = None
    name: Optional[str] = None
    category: Optional[Literal['locale', 'continent']] = None

    __table__: ClassVar[str] = """
        CREATE TABLE IF NOT EXISTS IgdbRegion (
            id INTEGER PRIMARY KEY,
            identifier TEXT COLLATE RTRIM,
            name TEXT COLLATE RTRIM,
            category TEXT COLLATE RTRIM
        );
    """

@dataclasses.dataclass(frozen=True, kw_only=True, slots=True)
class Keyword(IgdbObject):
    id: IgdbId
    name: str
    slug: str

    __table__: ClassVar[str] = """
        CREATE TABLE IF NOT EXISTS IgdbKeyword (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            slug TEXT NOT NULL COLLATE RTRIM
        );
    """

@dataclasses.dataclass(frozen=True, kw_only=True, slots=True)
class Language(IgdbObject):
    id: IgdbId
    locale: str
    name: str

    __table__: ClassVar[str] = """
        CREATE TABLE IF NOT EXISTS IgdbLanguage (
            id INTEGER PRIMARY KEY,
            locale TEXT NOT NULL,
            name TEXT NOT NULL
        );
    """

@dataclasses.dataclass(frozen=True, kw_only=True, slots=True)
class LanguageSupportType(IgdbObject):
    id: IgdbId
    name: str

    __table__: ClassVar[str] = """
        CREATE TABLE IF NOT EXISTS IgdbLanguageSupportType (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL
        );
    """

@dataclasses.dataclass(frozen=True, kw_only=True, slots=True)
class LanguageSupport(IgdbObject):
    id: IgdbId
    language: Language
    language_support_type: LanguageSupportType

    __table__: ClassVar[str] = """
        CREATE TABLE IF NOT EXISTS IgdbLanguageSupport (
            id INTEGER PRIMARY KEY,
            language INTEGER NOT NULL REFERENCES IgdbLanguage(id),
            language_support_type INTEGER NOT NULL REFERENCES IgdbLanguageSupportType(id)
        );
    """

    @override
    def to_row(self) -> IgdbIndexRow:
        return {
            'id': self.id,
            'language': self.language.id,
            'language_support_type': self.language_support_type.id,
        }

@dataclasses.dataclass(frozen=True, kw_only=True, slots=True)
class PlatformFamily(IgdbObject):
    id: IgdbId
    name: str

    __table__: ClassVar[str] = """
        CREATE TABLE IF NOT EXISTS IgdbPlatformFamily (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL COLLATE RTRIM
        );
    """

@dataclasses.dataclass(frozen=True, kw_only=True, slots=True)
class PlatformType(IgdbObject):
    id: IgdbId
    name: str

    __table__: ClassVar[str] = """
        CREATE TABLE IF NOT EXISTS IgdbPlatformType (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL COLLATE RTRIM
        );
    """

@dataclasses.dataclass(frozen=True, kw_only=True, slots=True)
class PlatformVersion(IgdbObject):
    id: IgdbId
    name: str
    slug: str

    __table__: ClassVar[str] = """
        CREATE TABLE IF NOT EXISTS IgdbPlatformVersion (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL COLLATE RTRIM,
            slug TEXT NOT NULL COLLATE RTRIM
        );
    """

@dataclasses.dataclass(frozen=True, kw_only=True, slots=True)
class Platform(IgdbObject):
    id: IgdbId
    abbreviation: Optional[str] = None
    alternative_name: Optional[str] = None
    generation: Optional[int] = None
    name: str
    platform_family: Optional[PlatformFamily] = None
    platform_type: Optional[PlatformType] = None
    slug: Optional[str] = None
    summary: Optional[str] = None

    __table__: ClassVar[str] = """
        CREATE TABLE IF NOT EXISTS IgdbPlatform (
            id INTEGER PRIMARY KEY,
            abbreviation TEXT COLLATE RTRIM,
            alternative_name TEXT COLLATE RTRIM,
            generation INTEGER,
            name TEXT NOT NULL COLLATE RTRIM,
            platform_family INTEGER REFERENCES IgdbPlatformFamily(id),
            platform_type INTEGER REFERENCES IgdbPlatformType(id),
            slug TEXT COLLATE RTRIM,
            summary TEXT
        );
    """

    @override
    def to_row(self) -> IgdbIndexRow:
        return {
            'id': self.id,
            'abbreviation': self.abbreviation,
            'alternative_name': self.alternative_name,
            'generation': self.generation,
            'name': self.name,
            'platform_family': self.platform_family.id if self.platform_family else None,
            'platform_type': self.platform_type.id if self.platform_type else None,
            'slug': self.slug,
            'summary': self.summary,
        }

@dataclasses.dataclass(frozen=True, kw_only=True, slots=True)
class MultiplayerMode(IgdbObject):
    id: IgdbId
    campaigncoop: bool
    dropin: bool
    lancoop: bool
    offlinecoop: bool
    offlinecoopmax: Optional[int] = None
    offlinemax: Optional[int] = None
    onlinecoop: bool
    onlinecoopmax: Optional[int] = None
    onlinemax: Optional[int] = None
    platform: Optional[Platform] = None
    splitscreen: bool
    splitscreenonline: Optional[bool] = None

    __table__: ClassVar[str] = """
        CREATE TABLE IF NOT EXISTS IgdbMultiplayerMode (
            id INTEGER PRIMARY KEY,
            campaigncoop BOOLEAN NOT NULL,
            dropin BOOLEAN NOT NULL,
            lancoop BOOLEAN NOT NULL,
            offlinecoop BOOLEAN NOT NULL,
            offlinecoopmax INTEGER,
            offlinemax INTEGER,
            onlinecoop BOOLEAN NOT NULL,
            onlinecoopmax INTEGER,
            onlinemax INTEGER,
            platform INTEGER REFERENCES IgdbPlatform(id),
            splitscreen BOOLEAN NOT NULL,
            splitscreenonline BOOLEAN
        );
    """

    @override
    def to_row(self) -> IgdbIndexRow:
        return {
            'id': self.id,
            'campaigncoop': self.campaigncoop,
            'dropin': self.dropin,
            'lancoop': self.lancoop,
            'offlinecoop': self.offlinecoop,
            'offlinecoopmax': self.offlinecoopmax,
            'offlinemax': self.offlinemax,
            'onlinecoop': self.onlinecoop,
            'onlinecoopmax': self.onlinecoopmax,
            'onlinemax': self.onlinemax,
            'platform': self.platform.id if self.platform else None,
            'splitscreen': self.splitscreen,
            'splitscreenonline': self.splitscreenonline,
        }

    @property
    def coop(self) -> bool:
        return self.campaigncoop or self.lancoop or self.offlinecoop or self.onlinecoop

@dataclasses.dataclass(frozen=True, kw_only=True, slots=True)
class PlayerPerspective(IgdbObject):
    id: IgdbId
    name: str

    __table__: ClassVar[str] = """
        CREATE TABLE IF NOT EXISTS IgdbPlayerPerspective (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL COLLATE RTRIM
        );
    """

@dataclasses.dataclass(frozen=True, kw_only=True, slots=True)
class DateFormat(IgdbObject):
    id: IgdbId
    format: str

    __table__: ClassVar[str] = """
        CREATE TABLE IF NOT EXISTS IgdbDateFormat (
            id INTEGER PRIMARY KEY,
            format TEXT NOT NULL COLLATE RTRIM
        );
    """

@dataclasses.dataclass(frozen=True, kw_only=True, slots=True)
class ReleaseDateRegion(IgdbObject):
    id: IgdbId
    region: str

    __table__: ClassVar[str] = """
        CREATE TABLE IF NOT EXISTS IgdbReleaseDateRegion (
            id INTEGER PRIMARY KEY,
            region TEXT NOT NULL COLLATE RTRIM
        );
    """

@dataclasses.dataclass(frozen=True, kw_only=True, slots=True)
class ReleaseDateStatus(IgdbObject):
    id: IgdbId
    description: str
    name: str

    __table__: ClassVar[str] = """
        CREATE TABLE IF NOT EXISTS IgdbReleaseDateStatus (
            id INTEGER PRIMARY KEY,
            description TEXT NOT NULL,
            name TEXT NOT NULL
        );
    """

@dataclasses.dataclass(frozen=True, kw_only=True, slots=True)
class ReleaseDate(IgdbObject):
    id: IgdbId
    date: Optional[int] = None
    date_format: DateFormat
    human: str
    m: Optional[Literal[1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12]] = None  # Month (1-12)
    platform: Platform
    release_region: ReleaseDateRegion
    status: Optional[ReleaseDateStatus] = None
    y: Optional[int] = None  # Year

    __table__: ClassVar[str] = """
        CREATE TABLE IF NOT EXISTS IgdbReleaseDate (
            id INTEGER PRIMARY KEY,
            date INTEGER,
            date_format INTEGER NOT NULL REFERENCES IgdbDateFormat(id),
            human TEXT,
            m INTEGER,
            platform INTEGER NOT NULL REFERENCES IgdbPlatform(id),
            release_region INTEGER NOT NULL REFERENCES IgdbReleaseDateRegion(id),
            status INTEGER REFERENCES IgdbReleaseDateStatus(id),
            y INTEGER
        );
    """

    @override
    def to_row(self) -> IgdbIndexRow:
        return {
            'id': self.id,
            'date': self.date,
            'date_format': self.date_format.id,
            'human': self.human,
            'm': self.m,
            'platform': self.platform.id,
            'release_region': self.release_region.id,
            'status': self.status.id if self.status else None,
            'y': self.y,
        }

@dataclasses.dataclass(frozen=True, kw_only=True, slots=True)
class Theme(IgdbObject):
    id: IgdbId
    name: str

    __table__: ClassVar[str] = """
        CREATE TABLE IF NOT EXISTS IgdbTheme (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL
        );
    """

@dataclasses.dataclass(frozen=True, kw_only=True, slots=True)
class Game(IgdbObject):
    id: IgdbId
    age_ratings: Optional[Sequence[AgeRating]] = None
    aggregated_rating: Optional[float] = None
    aggregated_rating_count: Optional[int] = None
    alternative_names: Optional[Sequence[AlternativeName]] = None
    bundles: Optional[Sequence['Game']] = None # name, ID, and platform
    collections: Optional[Sequence['Game']] = None # name, ID, and platform
    dlcs: Optional[Sequence['Game']] = None # name, ID, and platform
    expanded_games: Optional[Sequence['Game']] = None # name, ID, and platform
    expansions: Optional[Sequence['Game']] = None # name, ID, and platform
    first_release_date: Optional[int] = None # TODO: Parse with date.fromtimestamp()
    forks: Optional[Sequence['Game']] = None # name, ID, and platform
    franchise: Optional[Franchise] = None
    franchises: Optional[Sequence[Franchise]] = None
    game_engines: Optional[Sequence[GameEngine]] = None
    game_localizations: Optional[Sequence[GameLocalization]] = None
    game_modes: Optional[Sequence[GameMode]] = None
    game_status: Optional[GameStatus] = None
    game_type: Optional[GameType] = None
    genres: Optional[Sequence[Genre]] = None
    involved_companies: Optional[Sequence[InvolvedCompany]] = None
    keywords: Optional[Sequence[Keyword]] = None
    language_supports: Optional[Sequence[LanguageSupport]] = None
    multiplayer_modes: Optional[Sequence[MultiplayerMode]] = None
    name: str
    parent_game: Optional['Game'] = None
    platforms: Optional[Sequence[Platform]] = None
    player_perspectives: Optional[Sequence[PlayerPerspective]] = None
    ports: Optional[Sequence['Game']] = None
    release_dates: Optional[Sequence[ReleaseDate]] = None
    remakes: Optional[Sequence['Game']] = None
    remasters: Optional[Sequence['Game']] = None
    slug: Optional[str] = None
    standalone_expansions: Optional[Sequence['Game']] = None
    storyline: Optional[str] = None
    summary: Optional[str] = None
    themes: Optional[Sequence[Theme]] = None
    total_rating: Optional[float] = None
    total_rating_count: Optional[int] = None
    url: Optional[str] = None # TODO: Parse with urllib
    version_parent: Optional['Game'] = None
    version_title: Optional[str] = None

    def __post_init__(self) -> None:
        if self.age_ratings is not None and not isinstance(self.age_ratings, tuple):
            object.__setattr__(self, 'age_ratings', tuple(self.age_ratings))
        if self.alternative_names is not None and not isinstance(self.alternative_names, tuple):
            object.__setattr__(self, 'alternative_names', tuple(self.alternative_names))
        if self.bundles is not None and not isinstance(self.bundles, tuple):
            object.__setattr__(self, 'bundles', tuple(self.bundles))
        if self.collections is not None and not isinstance(self.collections, tuple):
            object.__setattr__(self, 'collections', tuple(self.collections))
        if self.dlcs is not None and not isinstance(self.dlcs, tuple):
            object.__setattr__(self, 'dlcs', tuple(self.dlcs))
        if self.expanded_games is not None and not isinstance(self.expanded_games, tuple):
            object.__setattr__(self, 'expanded_games', tuple(self.expanded_games))
        if self.expansions is not None and not isinstance(self.expansions, tuple):
            object.__setattr__(self, 'expansions', tuple(self.expansions))
        if self.forks is not None and not isinstance(self.forks, tuple):
            object.__setattr__(self, 'forks', tuple(self.forks))
        if self.franchises is not None and not isinstance(self.franchises, tuple):
            object.__setattr__(self, 'franchises', tuple(self.franchises))
        if self.game_engines is not None and not isinstance(self.game_engines, tuple):
            object.__setattr__(self, 'game_engines', tuple(self.game_engines))
        if self.game_localizations is not None and not isinstance(self.game_localizations, tuple):
            object.__setattr__(self, 'game_localizations', tuple(self.game_localizations))
        if self.game_modes is not None and not isinstance(self.game_modes, tuple):
            object.__setattr__(self, 'game_modes', tuple(self.game_modes))
        if self.genres is not None and not isinstance(self.genres, tuple):
            object.__setattr__(self, 'genres', tuple(self.genres))
        if self.involved_companies is not None and not isinstance(self.involved_companies, tuple):
            object.__setattr__(self, 'involved_companies', tuple(self.involved_companies))
        if self.keywords is not None and not isinstance(self.keywords, tuple):
            object.__setattr__(self, 'keywords', tuple(self.keywords))
        if self.language_supports is not None and not isinstance(self.language_supports, tuple):
            object.__setattr__(self, 'language_supports', tuple(self.language_supports))
        if self.multiplayer_modes is not None and not isinstance(self.multiplayer_modes, tuple):
            object.__setattr__(self, 'multiplayer_modes', tuple(self.multiplayer_modes))
        if self.platforms is not None and not isinstance(self.platforms, tuple):
            object.__setattr__(self, 'platforms', tuple(self.platforms))
        if self.player_perspectives is not None and not isinstance(self.player_perspectives, tuple):
            object.__setattr__(self, 'player_perspectives', tuple(self.player_perspectives))
        if self.ports is not None and not isinstance(self.ports, tuple):
            object.__setattr__(self, 'ports', tuple(self.ports))
        if self.release_dates is not None and not isinstance(self.release_dates, tuple):
            object.__setattr__(self, 'release_dates', tuple(self.release_dates))
        if self.remakes is not None and not isinstance(self.remakes, tuple):
            object.__setattr__(self, 'remakes', tuple(self.remakes))
        if self.remasters is not None and not isinstance(self.remasters, tuple):
            object.__setattr__(self, 'remasters', tuple(self.remasters))
        if self.standalone_expansions is not None and not isinstance(self.standalone_expansions, tuple):
            object.__setattr__(self, 'standalone_expansions', tuple(self.standalone_expansions))
        if self.themes is not None and not isinstance(self.themes, tuple):
            object.__setattr__(self, 'themes', tuple(self.themes))

    __table__: ClassVar[str] = """
        CREATE TABLE IF NOT EXISTS IgdbGame (
            id INTEGER PRIMARY KEY,
            aggregate_rating REAL,
            aggregated_rating_count INTEGER,
            first_release_date INTEGER,
            franchise INTEGER REFERENCES IgdbFranchise(id),
            game_status INTEGER REFERENCES IgdbGameStatus(id),
            game_type INTEGER REFERENCES IgdbGameType(id),
            name TEXT NOT NULL COLLATE RTRIM,
            parent_game INTEGER REFERENCES IgdbGame(id),
            slug TEXT COLLATE RTRIM,
            storyline TEXT,
            summary TEXT,
            total_rating REAL,
            total_rating_count INTEGER,
            url TEXT COLLATE RTRIM,
            version_parent INTEGER REFERENCES IgdbGame(id),
            version_title TEXT COLLATE RTRIM
        );
        CREATE TABLE IF NOT EXISTS IgdbGame_age_ratings (
            game INTEGER NOT NULL REFERENCES IgdbGame(id),
            age_rating INTEGER NOT NULL REFERENCES IgdbAgeRating(id),
            PRIMARY KEY (game, age_rating)
        );
        CREATE TABLE IF NOT EXISTS IgdbGame_alternative_names (
            game INTEGER NOT NULL REFERENCES IgdbGame(id),
            alternative_name INTEGER NOT NULL REFERENCES IgdbAlternativeName(id),
            PRIMARY KEY (game, alternative_name)
        );
        CREATE TABLE IF NOT EXISTS IgdbGame_bundles (
            game INTEGER NOT NULL REFERENCES IgdbGame(id),
            bundle INTEGER NOT NULL REFERENCES IgdbGame(id),
            PRIMARY KEY (game, bundle)
        );
        CREATE TABLE IF NOT EXISTS IgdbGame_collections (
            game INTEGER NOT NULL REFERENCES IgdbGame(id),
            collection INTEGER NOT NULL REFERENCES IgdbGame(id),
            PRIMARY KEY (game, collection)
        );
        CREATE TABLE IF NOT EXISTS IgdbGame_dlcs (
            game INTEGER NOT NULL REFERENCES IgdbGame(id),
            dlc INTEGER NOT NULL REFERENCES IgdbGame(id),
            PRIMARY KEY (game, dlc)
        );
        CREATE TABLE IF NOT EXISTS IgdbGame_expanded_games (
            game INTEGER NOT NULL REFERENCES IgdbGame(id),
            expanded_game INTEGER NOT NULL REFERENCES IgdbGame(id),
            PRIMARY KEY (game, expanded_game)
        );
        CREATE TABLE IF NOT EXISTS IgdbGame_expansions (
            game INTEGER NOT NULL REFERENCES IgdbGame(id),
            expansion INTEGER NOT NULL REFERENCES IgdbGame(id),
            PRIMARY KEY (game, expansion)
        );
        CREATE TABLE IF NOT EXISTS IgdbGame_forks (
            game INTEGER NOT NULL REFERENCES IgdbGame(id),
            fork INTEGER NOT NULL REFERENCES IgdbGame(id),
            PRIMARY KEY (game, fork)
        );
        CREATE TABLE IF NOT EXISTS IgdbGame_franchises (
            game INTEGER NOT NULL REFERENCES IgdbGame(id),
            franchise INTEGER NOT NULL REFERENCES IgdbFranchise(id),
            PRIMARY KEY (game, franchise)
        );
        CREATE TABLE IF NOT EXISTS IgdbGame_game_engines (
            game INTEGER NOT NULL REFERENCES IgdbGame(id),
            game_engine INTEGER NOT NULL REFERENCES IgdbGameEngine(id),
            PRIMARY KEY (game, game_engine)
        );
        CREATE TABLE IF NOT EXISTS IgdbGame_game_localizations (
            game INTEGER NOT NULL REFERENCES IgdbGame(id),
            game_localization INTEGER NOT NULL REFERENCES IgdbGameLocalization(id),
            PRIMARY KEY (game, game_localization)
        );
        CREATE TABLE IF NOT EXISTS IgdbGame_game_modes (
            game INTEGER NOT NULL REFERENCES IgdbGame(id),
            game_mode INTEGER NOT NULL REFERENCES IgdbGameMode(id),
            PRIMARY KEY (game, game_mode)
        );
        CREATE TABLE IF NOT EXISTS IgdbGame_genres (
            game INTEGER NOT NULL REFERENCES IgdbGame(id),
            genre INTEGER NOT NULL REFERENCES IgdbGenre(id),
            PRIMARY KEY (game, genre)
        );
        CREATE TABLE IF NOT EXISTS IgdbGame_involved_companies (
            game INTEGER NOT NULL REFERENCES IgdbGame(id),
            involved_company INTEGER NOT NULL REFERENCES IgdbInvolvedCompany(id),
            PRIMARY KEY (game, involved_company)
        );
        CREATE TABLE IF NOT EXISTS IgdbGame_keywords (
            game INTEGER NOT NULL REFERENCES IgdbGame(id),
            keyword INTEGER NOT NULL REFERENCES IgdbKeyword(id),
            PRIMARY KEY (game, keyword)
        );
        CREATE TABLE IF NOT EXISTS IgdbGame_language_supports (
            game INTEGER NOT NULL REFERENCES IgdbGame(id),
            language_support INTEGER NOT NULL REFERENCES IgdbLanguageSupport(id),
            PRIMARY KEY (game, language_support)
        );
        CREATE TABLE IF NOT EXISTS IgdbGame_multiplayer_modes (
            game INTEGER NOT NULL REFERENCES IgdbGame(id),
            multiplayer_mode INTEGER NOT NULL REFERENCES IgdbMultiplayerMode(id),
            PRIMARY KEY (game, multiplayer_mode)
        );
        CREATE TABLE IF NOT EXISTS IgdbGame_platforms (
            game INTEGER NOT NULL REFERENCES IgdbGame(id),
            platform INTEGER NOT NULL REFERENCES IgdbPlatform(id),
            PRIMARY KEY (game, platform)
        );
        CREATE TABLE IF NOT EXISTS IgdbGame_player_perspectives (
            game INTEGER NOT NULL REFERENCES IgdbGame(id),
            player_perspective INTEGER NOT NULL REFERENCES IgdbPlayerPerspective(id),
            PRIMARY KEY (game, player_perspective)
        );
        CREATE TABLE IF NOT EXISTS IgdbGame_ports (
            game INTEGER NOT NULL REFERENCES IgdbGame(id),
            port INTEGER NOT NULL REFERENCES IgdbGame(id),
            PRIMARY KEY (game, port)
        );
        CREATE TABLE IF NOT EXISTS IgdbGame_release_dates (
            game INTEGER NOT NULL REFERENCES IgdbGame(id),
            release_date INTEGER NOT NULL REFERENCES IgdbReleaseDate(id),
            PRIMARY KEY (game, release_date)
        );
        CREATE TABLE IF NOT EXISTS IgdbGame_remakes (
            game INTEGER NOT NULL REFERENCES IgdbGame(id),
            remake INTEGER NOT NULL REFERENCES IgdbGame(id),
            PRIMARY KEY (game, remake)
        );
        CREATE TABLE IF NOT EXISTS IgdbGame_remasters (
            game INTEGER NOT NULL REFERENCES IgdbGame(id),
            remaster INTEGER NOT NULL REFERENCES IgdbGame(id),
            PRIMARY KEY (game, remaster)
        );
        CREATE TABLE IF NOT EXISTS IgdbGame_standalone_expansions (
            game INTEGER NOT NULL REFERENCES IgdbGame(id),
            standalone_expansion INTEGER NOT NULL REFERENCES IgdbGame(id),
            PRIMARY KEY (game, standalone_expansion)
        );
        CREATE TABLE IF NOT EXISTS IgdbGame_themes (
            game INTEGER NOT NULL REFERENCES IgdbGame(id),
            theme INTEGER NOT NULL REFERENCES IgdbTheme(id),
            PRIMARY KEY (game, theme)
        );
    """

    @override
    def to_row(self) -> IgdbIndexRow:
        return {
            'id': self.id,
            'aggregate_rating': self.aggregated_rating,
            'aggregate_rating_count': self.aggregated_rating_count,
            'first_release_date': self.first_release_date,
            'franchise': self.franchise.id if self.franchise else None,
            'game_status': self.game_status.id if self.game_status else None,
            'game_type': self.game_type.id if self.game_type else None,
            'name': self.name,
            'parent_game': self.parent_game.id if self.parent_game else None,
            'slug': self.slug,
            'storyline': self.storyline,
            'summary': self.summary,
            'total_rating': self.total_rating,
            'total_rating_count': self.total_rating_count,
            'url': self.url,
            'version_parent': self.version_parent.id if self.version_parent else None,
            'version_title': self.version_title,
        }

    @override
    def to_relationships(self) -> IgdbIndexRelationships:
        def make_set(items: Optional[Sequence[IgdbObject]]):
            return {(self.id, item.id) for item in (items or ())}

        return {
            'age_ratings': make_set(self.age_ratings),
            'alternative_names': make_set(self.alternative_names),
            'bundles': make_set(self.bundles),
            'collections': make_set(self.collections),
            'dlcs': make_set(self.dlcs),
            'expanded_games': make_set(self.expanded_games),
            'expansions': make_set(self.expansions),
            'forks': make_set(self.forks),
            'franchises': make_set(self.franchises),
            'game_engines': make_set(self.game_engines),
            'game_localizations': make_set(self.game_localizations),
            'game_modes': make_set(self.game_modes),
            'genres': make_set(self.genres),
            'involved_companies': make_set(self.involved_companies),
            'keywords': make_set(self.keywords),
            'language_supports': make_set(self.language_supports),
            'multiplayer_modes': make_set(self.multiplayer_modes),
            'platforms': make_set(self.platforms),
            'player_perspectives': make_set(self.player_perspectives),
            'ports': make_set(self.ports),
            'release_dates': make_set(self.release_dates),
            'remakes': make_set(self.remakes),
            'remasters': make_set(self.remasters),
            'standalone_expansions': make_set(self.standalone_expansions),
            'themes': make_set(self.themes),
        }

DEFAULT_GAME_FIELD_TUPLE: tuple[str, ...] = (
    "age_ratings.organization.name",
    "age_ratings.rating_category.rating",
    "age_ratings.rating_content_descriptions.description_type.name",
    "age_ratings.rating_content_descriptions.description",
    "aggregated_rating_count",
    "aggregated_rating",
    "alternative_names.comment",
    "alternative_names.name",
    "bundles",
    "dlcs",
    "expanded_games.name",
    "expanded_games.platforms.name",
    "expanded_games",
    "expansions",
    "forks",
    "franchise.name",
    "franchises.name",
    "game_engines.name",
    "game_localizations.name",
    "game_localizations.region.category",
    "game_localizations.region.identifier",
    "game_localizations.region.name",
    "game_modes.name",
    "game_status.status",
    "game_type.type",
    "genres.name",
    "involved_companies.company.country",
    "involved_companies.company.name",
    "involved_companies.company.slug",
    "involved_companies.company.status.name",
    "involved_companies.developer",
    "involved_companies.porting",
    "involved_companies.publisher",
    "involved_companies.supporting",
    "keywords.name",
    "language_supports.language_support_type.name",
    "language_supports.language.locale",
    "language_supports.language.name",
    "multiplayer_modes.campaigncoop",
    "multiplayer_modes.dropin",
    "multiplayer_modes.lancoop",
    "multiplayer_modes.offlinecoop",
    "multiplayer_modes.offlinecoopmax",
    "multiplayer_modes.offlinemax",
    "multiplayer_modes.onlinecoop",
    "multiplayer_modes.onlinecoopmax",
    "multiplayer_modes.onlinemax",
    "multiplayer_modes.platform",
    "multiplayer_modes.splitscreen",
    "multiplayer_modes.splitscreenonline",
    "name",
    "parent_game",
    "platforms.abbreviation",
    "platforms.alternative_name",
    "platforms.generation",
    "platforms.name",
    "platforms.platform_family.name",
    "platforms.platform_type.name",
    "platforms.summary",
    "player_perspectives.name",
    "ports",
    "release_dates.date_format.format",
    "release_dates.date",
    "release_dates.human",
    "release_dates.m",
    "release_dates.platform.name",
    "release_dates.release_region.region",
    "release_dates.status.description",
    "release_dates.status.name",
    "release_dates.y",
    "remakes",
    "remasters",
    "standalone_expansions",
    "storyline",
    "summary",
    "themes.name",
    "url",
    "version_parent",
    "version_title",
)

IGDB_OBJECT_TYPES = (
    AgeRatingOrganization,
    AgeRatingCategory,
    AgeRatingContentDescriptionType,
    AgeRatingContentDescriptionV2,
    AgeRating,
    AlternativeName,
    Franchise,
    GameEngine,
    GameLocalization,
    GameMode,
    GameStatus,
    GameType,
    Genre,
    CompanyStatus,
    Company,
    InvolvedCompany,
    Region,
    Keyword,
    Language,
    LanguageSupportType,
    LanguageSupport,
    PlatformFamily,
    PlatformType,
    PlatformVersion,
    Platform,
    MultiplayerMode,
    PlayerPerspective,
    DateFormat,
    ReleaseDateRegion,
    ReleaseDateStatus,
    ReleaseDate,
    Theme,
    Game,
)

SortDirection = Literal['asc', 'desc']
DEFAULT_SORT: tuple[str, SortDirection] = ('name', 'asc')
QUERY_CLAUSE = r'(fields|f|exclude|x|where|w|limit|l|offset|o|sort|s|search)\s+([^;]+)\s*;'

class GameResponse(TypedDict, total=False):
    name: Required[str]

class MultiqueryResult(TypedDict):
    name: str
    count: NotRequired[int]
    result: NotRequired[GameResponse]

type MultiqueryResponse = list[MultiqueryResult]

GameResponseAdapter = TypeAdapter(GameResponse)
MultiqueryResponseAdapter = TypeAdapter(MultiqueryResponse)
MultiqueryResponseListAdapter = TypeAdapter(list[list[dict[str, JsonValue]]])

class CountResponse(TypedDict):
    count: int

@dataclass(kw_only=True, eq=True)
class Query:
    fields: Optional[tuple[str, ...]]
    exclude: Optional[tuple[str, ...]]
    where: Optional[str]
    limit: int
    offset: int
    sort: Optional[tuple[str, SortDirection]]
    search: Optional[str]

    def __init__(
            self,
            query: Optional[str] = None,
            *, # Force keyword arguments for clarity
            fields: Optional[Iterable[str] | str] = "*",
            exclude: Optional[Iterable[str] | str] = None,
            where: Optional[str] = None,
            limit: int = 10, # IGDB's default
            offset: int = 0, # IGDB's default
            sort: Optional[tuple[str, SortDirection]] = None,
            search: Optional[str] = None,
    ) -> None:
        if query is not None:
            # If given a query string, use it to override all other parameters.
            for match in re.finditer(QUERY_CLAUSE, query.strip(), re.IGNORECASE):
                clause_name = match.group(1).lower()
                clause_value = match.group(2).strip()

                match clause_name:
                    case 'fields' | 'f':
                        fields = tuple(f.strip() for f in clause_value.split(',') if f.strip())
                    case 'exclude' | 'x':
                        exclude = tuple(f.strip() for f in clause_value.split(',') if f.strip())
                    case 'where' | 'w':
                        where = clause_value
                    case 'limit' | 'l':
                        limit = int(clause_value)
                    case 'offset' | 'o':
                        offset = int(clause_value)
                    case 'sort' | 's':
                        # Parse sort field and direction
                        sort_parts = clause_value.split()
                        if len(sort_parts) >= 1:
                            sort_field: str = sort_parts[0]
                        else:
                            raise ValueError("Sort clause must specify a field")

                        if len(sort_parts) >= 2:
                            sort_direction = cast(SortDirection, sort_parts[1].lower().strip())
                            if sort_direction not in ('asc', 'desc'):
                                raise ValueError("Sort direction must be 'asc' or 'desc'")
                        else:
                            sort_direction = 'asc' # Default to ascending if not specified

                        sort = (sort_field, sort_direction)
                    case 'search':
                        search = clause_value.strip()

        match fields:
            case str():
                self.fields = tuple(f.strip(" ;") for f in fields.split(",") if f)
            case Iterable():
                self.fields = tuple(f.strip(" ;") for f in fields if f)
            case None:
                self.fields = None
            case _:
                raise TypeError(f"Expected fields to be str, Iterable[str], or None; got {type(fields).__name__}")

        match exclude:
            case str():
                self.exclude = tuple(f.strip() for f in exclude.split(","))
            case Iterable():
                self.exclude = tuple(f.strip() for f in exclude)
            case None:
                self.exclude = None
            case _:
                raise TypeError(f"Expected exclude to be str, Iterable[str], or None; got {type(exclude).__name__}")

        # TODO: Come up with some strongly-typed way to handle the `where` clause.
        #  (Gotta handle ANDs, ORs, NOTs, operators, etc.)
        match where:
            case str():
                self.where = where.strip()
            case None:
                self.where = None
            case _:
                raise TypeError(f"Expected where to be str or None; got {type(where).__name__}")

        self.limit = limit
        self.offset = offset

        if search and sort:
            raise ValueError("Cannot specify both search and sort in a query.")

        self.search = search
        self.sort = sort

    def query_pages(self, count: int, limit: int = 500) -> Iterator['Query']:
        for i in range(0, count, limit):
            yield Query(
                fields=self.fields,
                exclude=self.exclude,
                where=self.where,
                limit=limit,
                offset=i,
                sort=self.sort,
                search=self.search,
            )

    def __str__(self) -> str:
        clauses: list[str] = []
        if self.fields:
            clauses.append(f"fields {','.join(self.fields)};")

        if self.exclude:
            clauses.append(f"exclude {','.join(self.exclude)};")

        if self.where:
            clauses.append(f"where {self.where};")

        if self.limit is not None:
            clauses.append(f"limit {self.limit};")

        if self.offset is not None:
            clauses.append(f"offset {self.offset};")

        if self.sort:
            clauses.append(f"sort {self.sort[0]} {self.sort[1]};")

        if self.search:
            clauses.append(f"search \"{self.search}\";")

        return ''.join(clauses)

@dataclass
class Playlist:
    title: PlaylistTitle
    '''
    The title of the playlist,
    which is used as the filename for the playlist file.
    Usually follows the format "Manufacturer - Platform Name".
    '''

    alts: Sequence[str]
    '''
    Other names that the playlist might be known by.
    '''

    query: Query
    '''
    The IGDB query to use to fetch games for this playlist.
    '''

    hasheous_dirs: Sequence[str]
    '''
    The names of zero or more Hasheous dump files, excluding the zip suffix.
    '''

    def __init__(
            self,
            title: PlaylistTitle,
            hasheous: Optional[str | Iterable[str]] = None,
            alts: Optional[str | Iterable[str]] = None,
            *, # Force keyword arguments for clarity
            fields: Optional[Iterable[str] | str] = DEFAULT_GAME_FIELD_TUPLE,
            exclude: Optional[Iterable[str] | str] = None,
            where: Optional[str] = None,
            limit: int = 500,
            offset: int = 0,
            sort: Optional[tuple[str, SortDirection]] = DEFAULT_SORT,
            search: Optional[str] = None,
    ):
        self.title = title

        self.query = Query(
            fields=fields,
            exclude=exclude,
            where=where,
            limit=limit,
            offset=offset,
            sort=sort,
            search=search,
        )

        match hasheous:
            case str():
                self.hasheous_dirs = (hasheous,)
            case Iterable():
                self.hasheous_dirs = tuple(hasheous)
            case None:
                self.hasheous_dirs = ()
            case _:
                raise TypeError(f"Expected hasheous to be str, Iterable[str], or None; got {type(hasheous).__name__}")

        match alts:
            case str():
                self.alts = (alts,)
            case Iterable():
                self.alts = tuple(alts)
            case None:
                self.alts = ()
            case _:
                raise TypeError(f"Expected alts to be str, Iterable[str], or None; got {type(alts).__name__}")


    def query_pages(self, count: int, limit: int = 500) -> Iterator[Query]:
        for i in range(0, count, limit):
            yield Query(
                fields=self.query.fields,
                exclude=self.query.exclude,
                where=self.query.where,
                limit=limit,
                offset=i,
                sort=self.query.sort,
                search=self.query.search,
            )

MULTIQUERY_MAX = 10
MULTIQUERY_LIMIT = MULTIQUERY_MAX
'''
The maximum number of queries that IGDB allows in a single multiquery.
'''

MAX_ACTIVE_QUERIES = 8

MAX_QUERY_RATE = 4
MAX_QUERY_PERIOD = 1.0 / MAX_QUERY_RATE


class Multiquery:
    def __init__(self, queries: Mapping[str, tuple[str, Query]]):
        if len(queries) > MULTIQUERY_LIMIT:
            raise ValueError(f"Multiquery can only contain up to {MULTIQUERY_LIMIT} queries; got {len(queries)}")

        self.queries = dict(queries)

    def __str__(self) -> str:
        queries: list[str] = []
        for name, (endpoint, query) in self.queries.items():
            queries.append(f"query {endpoint} \"{name}\" {{ {query} }};")

        return '\n'.join(queries)

RETRY_CODES = (
    httpx.codes.REQUEST_TIMEOUT,
    httpx.codes.TOO_MANY_REQUESTS,
    httpx.codes.INTERNAL_SERVER_ERROR,
    httpx.codes.BAD_GATEWAY,
    httpx.codes.SERVICE_UNAVAILABLE,
    httpx.codes.GATEWAY_TIMEOUT,
)

class QueryClient:
    def __init__(self, client_id: str, client_secret: str, max_queries: int = MAX_ACTIVE_QUERIES, max_rate: int = MAX_QUERY_RATE):
        # Limit to 8 in-flight requests
        self.request_limit = asyncio.BoundedSemaphore(max_queries)
        self.rate_limit = asynciolimiter.StrictLimiter(max_rate)
        self.client_id = client_id
        self.client_secret = client_secret
        self.client = AsyncOAuth2Client(
            client_id=client_id,
            client_secret=client_secret,
            token_endpoint='https://id.twitch.tv/oauth2/token',
            token_endpoint_auth_method='client_secret_post',
            scope=['user_read', 'user_subscriptions'],
        )

    async def __aenter__(self) -> Self:
        try:
            client = await self.client.__aenter__()

            token: OAuth2Token = await self.client.fetch_token(
                grant_type='client_credentials',
                url='https://id.twitch.tv/oauth2/token',
                client_id=self.client_id,
                client_secret=self.client_secret,
            )

            if not token:
                raise RuntimeError("Failed to obtain access token from Twitch")

            return self
        except:
            await self.client.__aexit__(None, None, None)
            raise

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        await self.client.__aexit__(exc_type, exc_val, exc_tb)


    @staticmethod
    def _on_backoff(details):
        print("Retrying after backoff:", details['target'].__name__, "with args:", details['args'], "and kwargs:", details['kwargs'], file=sys.stderr)

    @staticmethod
    def _on_predicate(response: Response) -> bool:
        status = response.status_code
        if status in RETRY_CODES:
            # If the response is a retryable error, we want to retry
            print(f"Retrying due to status code {status} ({response.reason_phrase})", file=sys.stderr)
            return True

        return False


    @staticmethod
    def _giveup(e: Exception):
        if not isinstance(e, HTTPStatusError):
            # Give up if query_endpoint failed with something besides HTTPStatusError
            return True

        if e.response.status_code in RETRY_CODES:
            # Don't give up on server errors (5xx), we might just be unlucky
            # or rate-limited (429), so we should back off and retry.
            return False

        return e.response.is_error

    @backoff.on_exception(backoff.expo, HTTPStatusError, max_tries=5, giveup=_giveup, on_backoff=_on_backoff)
    @backoff.on_predicate(backoff.expo, _on_predicate)
    async def _query(self, endpoint: str, query: str | Query | Multiquery) -> Response:
        """
        Query the IGDB API with the given endpoint and query.

        Args:
            client: The authenticated AsyncOAuth2Client instance
            endpoint: The IGDB API endpoint to query
            query: The Apicalypse query string to send to the endpoint

        Returns:
            The HTTP response from the API

        Raises:
            requests.exceptions.RequestException: If the request fails
        """

        async with self.request_limit:
            url = f"https://api.igdb.com/v4/{endpoint}"
            access_token = self.client.token["access_token"]

            headers = {
                'Client-ID': self.client.client_id,
                'Authorization': f'Bearer {access_token}',
                'Accept': 'application/json',
                'Accept-Encoding': 'gzip, deflate'
            }

            await self.rate_limit.wait()
            response = await self.client.post(url, headers=headers, content=str(query), timeout=Timeout(None))
            response.raise_for_status()

            content_type = response.headers.get("Content-Type")

            if response.headers.get('content-type') != 'application/json':
                raise ValueError(f"Expected IGDB query response to be JSON, got: {content_type} ({response.text})")

            return response

    @overload
    async def query(self, endpoint: Literal["multiquery"], query: str | Multiquery) -> JsonValue: ...

    @overload
    async def query(self, endpoint: Literal["multiquery"], query: Query) -> Never: ...

    @overload
    async def query(self, endpoint: str, query: str | Query | Multiquery) -> JsonValue: ...

    async def query(self, endpoint: str, query: str | Query | Multiquery) -> JsonValue:
        if endpoint == "multiquery" and isinstance(query, Query):
            raise TypeError("Expected a str or Multiquery for 'multiquery' endpoint; got Query")

        try:
            response = await self._query(endpoint, query)
            return from_json(response.content)
        except HTTPStatusError as e:
            if not (isinstance(query, Multiquery) and e.response.status_code == httpx.codes.REQUEST_ENTITY_TOO_LARGE):
                # If the error is not due to multiquery size limit, re-raise
                raise

            print(f"Multiquery too large (HTTP 413); splitting into {len(query.queries)} individual queries...", file=sys.stderr)

            return await self._split_multiquery(query)
            # MultiqueryResponse is a list[TypedDict], which is suitable as a JsonValue

    async def _split_multiquery(self, multiquery: Multiquery) -> list[JsonValue]:
        tasks: list[Task[JsonValue]] = []

        async with asyncio.TaskGroup() as group:
            for name, (query_endpoint, query_obj) in multiquery.queries.items():
                tasks.append(group.create_task(
                    self.query(query_endpoint, query_obj),
                    name=name
                ))

            # Wait for all individual queries to complete
            # This is better than sequential execution because we can still benefit from concurrency
            results = await asyncio.gather(*tasks)
            print(f"Completed {len(results)} individual queries (split from oversized multiquery)", file=sys.stderr)
            response: list[JsonValue] = []
            for task in tasks:
                response.append({
                    "name": task.get_name(),
                    "result": task.result(),
                })

            return response

    async def count(self, endpoint: str, query: str | Query) -> int:
        if not endpoint.endswith('/count'):
            endpoint += '/count'

        response = await self._query(endpoint, query)
        response_json = from_json(response.content)

        if not isinstance(response_json, Mapping):
            raise ValueError(f"Expected {endpoint} response to be a JSON object, got {type(response_json)} ({response_json})")

        if 'count' not in response_json:
            # If the response is successful yet wrong, raise a ValueError
            raise ValueError(f"Expected a 'count' attribute in response from {endpoint}, got {response_json}")

        count = response_json['count']
        if not isinstance(count, int):
            raise ValueError(f"Expected response['count'] to be a number, got {type(count)}")

        return int(count)

def read_playlists(path: str) -> tuple[Playlist, ...]:
    class TomlPlaylistEntry(TypedDict):
        title: PlaylistTitle
        hasheous: Sequence[str]
        alts: Sequence[str]
        where: str

    with open(path, "rb") as playlist_file:
        toml = tomllib.load(playlist_file)

        if not (igdb := toml.get('igdb')):
            raise KeyError(f"Missing 'igdb' section in TOML file at {path}")

        if not (playlists := igdb.get('playlists')):
            raise KeyError(f"Missing 'playlists' array in 'igdb' table of TOML file at {path}")

        if not isinstance(playlists, list):
            raise TypeError(f"Expected 'playlists' to be a list; got {type(playlists).__name__}")

        playlist_objects = cast(Sequence[TomlPlaylistEntry], playlists)

        def load_playlist(entry: TomlPlaylistEntry) -> Playlist:
            # We use a separate function so that the Playlist is hashable
            # (as tomllib loads into mutable dicts and lists)
            return Playlist(
                title=entry['title'],
                hasheous=tuple(entry.get('hasheous', ())),
                alts=tuple(entry.get('alts', ())),
                where=entry['where'],
            )
        return tuple(load_playlist(p) for p in playlist_objects)


dirname = os.path.dirname(__file__)
TOML_PATH = os.path.normpath(os.path.join(os.path.dirname(__file__), '..', 'metadat', 'igdb', 'igdb.toml'))

PLAYLISTS = read_playlists(TOML_PATH)

PLAYLISTS_BY_TITLE = {str(p.title): p for p in PLAYLISTS}
PLAYLISTS_BY_TITLE_LOWER = {p.title.lower(): p for p in PLAYLISTS}
PLAYLISTS_BY_ANY: Mapping[str, Playlist] = ChainMap(
    PLAYLISTS_BY_TITLE,
    PLAYLISTS_BY_TITLE_LOWER,
)
PLAYLIST_TITLES = tuple(p.title for p in PLAYLISTS)

ANALOG_KEYWORD_IDS = (
    4965, # circle pad pro support
    10740, # gamecube
    11394, # gamecube controller support on wii
    45794, # n64 controller supported
    48530, # input type - dial controls
)

KEYWORD_OVERRIDES = {
    1173: 27627, # "james bond" -> "007"
    42455: 893, # "1500s" -> "16th Century"
    3552: 535, # "1990's" -> "1990s"
    41008: 50517, # "1bit" -> "1-bit"
    44939: 589, # "25d" -> "2.5d"
    3696: 25392, # "2d fighter" -> "2d fighting"
    47978: 3014, # "2d-side-scroller" -> "2d platformer"

    18448: 2231, # "3d platform" -> "3d platformer"
    16964: 128, # "80s" -> "1980s"
    7294: 128, # "the 1980s" -> "1980s"

    50684: 3079, # "8bit" -> "8-bit"

}

RUMBLE_KEYWORD_IDS = (
    8156, # contextual controller rumble
    50485, # game boy player rumble support
    46206, # nintendo ds rumble pak
    10564, # rumble cartridge
    27048, # rumble pak
    38907, # rumble support
)

@cache
def get_playlist(identifier: str | Path) -> Optional[Playlist]:
    """
    Look up a playlist in `PLAYLISTS` by its title, path, or alternative name.
    """

    if isinstance(identifier, Path):
        playlist_id = identifier.stem.lower()
    else:
        playlist_id = identifier.strip().lower()

    for playlist in PLAYLISTS:
        if playlist_id == playlist.title.lower():
            return playlist

        if any(playlist_id == alt.lower() for alt in playlist.alts):
            return playlist

        if any(playlist_id == h.lower() for h in playlist.hasheous_dirs):
            return playlist

    return None


def get_by_title(title: str) -> Optional[Playlist]:
    """
    Get a playlist by its title.

    :param title: The title of the playlist to search for.
    :return: The Playlist object if found, otherwise None.
    """

    for playlist in PLAYLISTS:
        if playlist.title.lower() == title.lower():
            return playlist

    return None


GameTupleCodec: typelib.Codec[tuple[Game, ...]] = typelib.codec(tuple[Game, ...])

def get_client_credentials(args: argparse.Namespace) -> tuple[str, str]:
    """Get client ID and secret from args or environment variables."""
    client_id = args.client_id or os.getenv('TWITCH_CLIENT_ID')
    client_secret = args.client_secret or os.getenv('TWITCH_CLIENT_SECRET')

    if not client_id or not client_secret:
        raise ValueError("Client ID and Client Secret are required for authentication")

    return client_id, client_secret


def load_file(path: Path, playlist: Playlist) -> tuple[PlaylistTitle, Collection[Game]]:
    with open(path, mode='rb') as infile:
        json_bytes = infile.read()
        games = GameTupleCodec.decode(json_bytes)
        return playlist.title, games

class IgdbIndex:
    def __init__(self, games: Iterable[tuple[PlaylistTitle, Iterable[Game]]]):
        playlists_iterators = dict(games)
        playlists = {title: tuple(obj_iter) for title, obj_iter in playlists_iterators.items()}
        self.by_playlist = playlists
        self.by_id: dict[IgdbId, Game] = {}

        for game in itertools.chain.from_iterable(playlists.values()):
            self.by_id[game.id] = game

    @property
    def by_igdb_id(self):
        return self.by_id

async def load_games(playlists: Mapping[Path, Playlist], executor: Executor) -> IgdbIndex:
    """
    :param playlists: An iterable of tuples,
    where each tuple contains the path to a playlist file
    and the corresponding Playlist object.

    :return: A mapping of playlist titles to collections of the Games they represent.
    """

    loop = asyncio.get_running_loop()
    futures = (loop.run_in_executor(executor, load_file, k, v) for (k, v) in playlists.items())

    return IgdbIndex(await asyncio.gather(*futures))


async def load_game_file(path: Path, playlist: Playlist) -> tuple[PlaylistTitle, Collection[Game]]:
    async with aiofiles.open(path, mode='rb') as infile:
        json_bytes = await infile.read()
        games = GameTupleCodec.decode(json_bytes)
        return playlist.title, games

async def handle_query(args: argparse.Namespace) -> None:
    """Handle the query subcommand."""

    all_records = bool(args.all)
    verbose = bool(args.verbose)

    if args.endpoint == "multiquery":
        # Read multiquery definitions from file or stdin
        if args.query == '-':
            body = sys.stdin.read()
        else:
            with open(args.query, 'r') as f:
                body = f.read()
    else:
        body = args.query

    client_id, client_secret = get_client_credentials(args)
    async with QueryClient(client_id, client_secret) as client:
        try:
            if not all_records:
                # If the user didn't pass the --all flag...
                response = await client.query(args.endpoint, body)
                json = orjson.dumps(response, option=orjson.OPT_INDENT_2 | orjson.OPT_APPEND_NEWLINE)
                await aiofiles.stdout_bytes.write(json)
            else:
                count_response = cast(CountResponse, await client.query(f"{args.endpoint}/count", body))
                count = count_response["count"]
                if verbose:
                    print(f"Query will return {count} total records", file=sys.stderr)

                query = Query(body)
                async with asyncio.TaskGroup() as group:
                    tasks: list[Task[JsonValue]] = []
                    for q in itertools.batched(query.query_pages(count), MULTIQUERY_MAX):
                        if verbose:
                            print(f"Fetching records {q[0].offset} to {q[-1].offset + q[-1].limit - 1}", file=sys.stderr)

                        multiquery = Multiquery({f"{args.endpoint} ({p.offset}-{p.offset + p.limit - 1})": (args.endpoint, p) for p in q})
                        task = group.create_task(client.query("multiquery", multiquery))
                        tasks.append(task)

                    responses = await asyncio.gather(*tasks)

                multiquery_responses = MultiqueryResponseListAdapter.validate_python(responses, extra='allow')
                results = tuple(itertools.chain.from_iterable(multiquery_responses))
                json = to_json(results, indent=2)
                await aiofiles.stdout_bytes.write(json)
        except JSONDecodeError as e:
            print(e.doc, file=sys.stderr)
            print(e, file=sys.stderr)
            raise e


async def handle_fetch(args: argparse.Namespace) -> None:
    """Handle the fetch subcommand."""

    playlist_args: Iterable[str] | None = args.playlist
    if not playlist_args:
        # If no playlists specified, use all known playlists
        playlist_args = (p.title for p in PLAYLISTS)

    # Get all playlists to scrape (filter out the Nones)
    playlists = tuple(filter(None, (get_playlist(p) for p in playlist_args)))
    if not playlists:
        raise ValueError("All listed playlists are unknown.")

    outdir: str = args.outdir

    await aiofiles.os.makedirs(outdir, exist_ok=True)

    async def fetch_playlist(client: QueryClient, playlist: Playlist, group: TaskGroup) -> Sequence[GameResponse]:
        print(f"{playlist.title}: Fetching game count in query...")
        count = await client.count("games", playlist.query)

        multiqueries: list[Multiquery] = []
        for batch in itertools.batched(playlist.query_pages(count), MULTIQUERY_MAX):
            multiqueries.append(Multiquery({f"{playlist.title} ({q.offset}-{q.offset + q.limit - 1})": ('games', q) for q in batch}))

        playlist_tasks = tuple(group.create_task(client.query("multiquery", m)) for m in multiqueries)
        print(f"{playlist.title}: Scheduled to fetch {count} games...")

        responses: Sequence[JsonArray]  = await asyncio.gather(*playlist_tasks)
        games: list[GameResponse] = []

        for r in responses:
            if not isinstance(r, Sequence):
                raise ValueError(f"Expected multiquery response for '{playlist.title}' to be a JSON array; got: {type(r)} ({r})")

            for g in cast(Sequence[MultiqueryResponse], r):
                games.extend(g['result'])
                # We're not processing the returned games except to sort them,
                # so we don't need to convert them to IgdbGame objects here.

        print(f"{playlist.title}: Fetched {len(games)} games.")
        games.sort(key=lambda g: g['name'])
        # Now that we have all the games, sort them by name

        # Create the output directory if it doesn't exist
        await aiofiles.os.makedirs(outdir, exist_ok=True)
        outpath = os.path.join(outdir, f"{playlist.title}.json")
        async with aiofiles.open(outpath, 'wb') as outfile:
            json = orjson.dumps(games, option=orjson.OPT_INDENT_2 | orjson.OPT_APPEND_NEWLINE)
            await outfile.write(json)
            print(f"{playlist.title}: Saved {len(games)} games to {outpath}")

        return games

    client_id, client_secret = get_client_credentials(args)
    async with QueryClient(client_id, client_secret) as client:
        async with asyncio.TaskGroup() as group:
            tasks = tuple(group.create_task(fetch_playlist(client, p, group), name=p.title) for p in playlists)

def main():
    """Main entry point for the script."""

    parser = argparse.ArgumentParser(
        description="Utilities for fetching and processing data from IGDB.",
        epilog="See https://api-docs.igdb.com for more information about the IGDB API and its query syntax."
    )

    parser.add_argument(
        "--verbose",
        action="store_true",
        help="show more logging output"
    )

    subparsers = parser.add_subparsers(
        dest="command",
        help="Available commands",
        required=True
    )

    # Query subcommand
    query_parser = subparsers.add_parser(
        "query",
        help="Make a request to an IGDB API endpoint and print the response to stdout."
    )
    query_parser.add_argument(
        "--client-id",
        type=str,
        help="Your IGDB API client ID. Overrides the TWITCH_CLIENT_ID environment variable if provided.",
        default=None,
    )
    query_parser.add_argument(
        "--client-secret",
        type=str,
        help="Your IGDB API client secret. Overrides the TWITCH_CLIENT_SECRET environment variable if provided."
    )
    query_parser.add_argument(
        "--all",
        action="store_true",
        help="Use this query, but ignore the 'offset'/'limit' clauses and fetch all results."
    )
    query_parser.add_argument(
        "endpoint",
        type=str,
        help="The IGDB API endpoint to query."
    )
    query_parser.add_argument(
        "query",
        type=str,
        help="The Apicalypse query to query data from. If 'endpoint' is 'multiquery', this should be a path to a query file or '-' to read from stdin."
    )
    query_parser.set_defaults(func=handle_query)

    # fetch subcommand
    fetch_parser = subparsers.add_parser(
        "fetch",
        help="Fetch data from IGDB and save it to the specified directory"
    )
    fetch_parser.add_argument(
        "--client-id",
        type=str,
        help="The IGDB API client ID. Overrides the TWITCH_CLIENT_ID environment variable if provided."
    )
    fetch_parser.add_argument(
        "--client-secret",
        type=str,
        help="The IGDB API client secret. Overrides the TWITCH_CLIENT_SECRET environment variable if provided."
    )
    fetch_parser.add_argument(
        "--playlist",
        type=str,
        help="The titles of the playlists to scrape. If not provided, all known playlists will be scraped.",
        action="extend",
        nargs="*",
        default=PLAYLISTS_BY_TITLE.keys()  # Default to all known playlists
    )
    fetch_parser.add_argument(
        "outdir",
        type=str,
        help="The output directory for the scraped JSON files",
        default="tmp/igdb",
    )
    fetch_parser.set_defaults(func=handle_fetch)

    # Parse arguments and call appropriate handler

    args = parser.parse_args()
    asyncio.run(args.func(args))

__all__ = (
    "AgeRating",
    "AgeRatingCategory",
    "AgeRatingContentDescriptionType",
    "AgeRatingContentDescriptionV2",
    "AgeRatingOrganization",
    "AlternativeName",
    "ANALOG_KEYWORD_IDS",
    "Company",
    "CompanyStatus",
    "DateFormat",
    "DEFAULT_GAME_FIELD_TUPLE",
    "DEFAULT_SORT",
    "Franchise",
    "Game",
    "GameEngine",
    "GameLocalization",
    "GameMode",
    "GameStatus",
    "GameType",
    "Genre",
    "get_by_title",
    "get_playlist",
    "IgdbIndex",
    "IgdbId",
    "IgdbObject",
    "IGDB_OBJECT_TYPES",
    "InvolvedCompany",
    "Keyword",
    "Language",
    "LanguageSupport",
    "LanguageSupportType",
    "load_games",
    "MAX_ACTIVE_QUERIES",
    "MAX_QUERY_PERIOD",
    "MAX_QUERY_RATE",
    "MultiplayerMode",
    "MULTIQUERY_MAX",
    "Multiquery",
    "Platform",
    "PlatformFamily",
    "PlatformType",
    "PlatformVersion",
    "PlayerPerspective",
    "PLAYLIST_TITLES",
    "Playlist",
    "PLAYLISTS_BY_TITLE",
    "PLAYLISTS",
    "PlaylistTitle",
    "Query",
    "QueryClient",
    "Region",
    "ReleaseDate",
    "ReleaseDateRegion",
    "ReleaseDateStatus",
    "RUMBLE_KEYWORD_IDS",
    "SortDirection",
    "Theme",
)

if __name__ == "__main__":
    main()
