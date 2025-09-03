import os.path
import tomllib

from collections import ChainMap
from dataclasses import dataclass
from typing import Optional, Literal, TypedDict, Required, NewType, cast
from collections.abc import Sequence, Iterable, Iterator, Mapping

DEFAULT_GAME_FIELD_TUPLE: tuple[str, ...] = (
    "age_ratings.organization.name",
    "age_ratings.rating_category.rating",
    "age_ratings.rating_content_descriptions.description",
    "age_ratings.rating_content_descriptions.description_type.name",
    "aggregated_rating",
    "aggregated_rating_count",
    "alternative_names.comment",
    "alternative_names.name",
    "bundles.name",
    "collections.name",
    "collections.type.name",
    "dlcs",
    "dlcs.name",
    "expanded_games",
    "expanded_games.platforms.name",
    "expanded_games.name",
    "expansions",
    "expansions.name",
    "expansions.platforms.name",
    "first_release_date",
    "forks.name",
    "forks.platforms.name",
    "franchise.name",
    "franchise.slug",
    "franchises.name",
    "franchises.slug",
    "game_engines.name",
    "game_engines.slug",
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
    "keywords.slug",
    "language_supports.language.locale",
    "language_supports.language.name",
    "language_supports.language_support_type.name",
    "multiplayer_modes.campaigncoop",
    "multiplayer_modes.dropin",
    "multiplayer_modes.lancoop",
    "multiplayer_modes.offlinecoop",
    "multiplayer_modes.offlinecoopmax",
    "multiplayer_modes.offlinemax",
    "multiplayer_modes.onlinecoop",
    "multiplayer_modes.onlinecoopmax",
    "multiplayer_modes.onlinemax",
    "multiplayer_modes.platform.name",
    "multiplayer_modes.splitscreen",
    "multiplayer_modes.splitscreenonline",
    "name",
    "parent_game.name",
    "platforms.abbreviation",
    "platforms.alternative_name",
    "platforms.generation",
    "platforms.name",
    "platforms.platform_family.name",
    "platforms.platform_type.name",
    "platforms.slug",
    "platforms.summary",
    # "platforms.versions.name",
    # "platforms.versions.slug",
    "player_perspectives.name",
    "ports.name",
    "ports.platforms.name",
    "release_dates.date",
    "release_dates.date_format.format",
    "release_dates.human",
    "release_dates.m",
    "release_dates.platform.name",
    "release_dates.release_region.region",
    "release_dates.status.description",
    "release_dates.status.name",
    "release_dates.y",
    "remakes.name",
    "remakes.platforms.name",
    "remasters.name",
    "remasters.platforms.name",
    "slug",
    "standalone_expansions.name",
    "standalone_expansions.platforms.name",
    "storyline",
    "summary",
    "themes.name",
    "total_rating",
    "total_rating_count",
    "version_title",
    "version_parent.name",
    "version_parent.platforms.name",
)
DEFAULT_GAME_FIELDS = ''.join(DEFAULT_GAME_FIELD_TUPLE)

IgdbId = NewType('IgdbId', int)

class IgdbObject(TypedDict):
    checksum: Optional[str] # For the object itself, *not* for a specific game
    id: Required[IgdbId]

class AgeRatingCategory(IgdbObject, total=False):
    organization: 'AgeRatingOrganization'
    rating: str

class AgeRatingContentDescriptionType(IgdbObject, total=False):
    name: str

class AgeRatingContentDescriptionV2(IgdbObject, total=False):
    description: str
    description_type: 'AgeRatingContentDescriptionType'

class AgeRatingOrganization(IgdbObject, total=False):
    name: str

class AgeRating(IgdbObject, total=False):
    content_descriptions: Sequence['AgeRatingContentDescriptionV2']
    organization: AgeRatingOrganization
    rating_category: 'AgeRatingCategory'
    rating_content_descriptions: Sequence['AgeRatingContentDescriptionV2']
    rating_cover_url: str
    synopsis: str

class AlternativeName(IgdbObject, total=False):
    name: str
    comment: str

class Franchise(IgdbObject, total=False):
    name: str
    slug: str

class GameEngine(IgdbObject, total=False):
    name: str
    slug: str

class GameLocalization(IgdbObject, total=False):
    name: str
    region: 'Region'

class GameMode(IgdbObject, total=False):
    name: str
    slug: str

class GameStatus(IgdbObject, total=False):
    status: str

class GameType(IgdbObject, total=False):
    type: str

class Genre(IgdbObject, total=False):
    name: str
    slug: str

class CompanyStatus(IgdbObject, total=False):
    name: str

class Company(IgdbObject, total=False):
    country: int # ISO 3166-1 code
    name: str
    slug: str
    status: CompanyStatus

class InvolvedCompany(IgdbObject, total=False):
    company: Company
    developer: bool
    porting: bool
    publisher: bool
    supporting: bool

class Region(IgdbObject, total=False):
    identifier: str
    name: str
    category: Literal['locale', 'continent']

class Keyword(IgdbObject, total=False):
    name: str
    slug: str

class Language(IgdbObject, total=False):
    locale: str
    name: str
    native_name: str

class LanguageSupportType(IgdbObject, total=False):
    name: str

class LanguageSupport(IgdbObject, total=False):
    language: Language
    language_support_type: LanguageSupportType

class MultiplayerMode(IgdbObject, total=False):
    campaigncoop: bool
    dropin: bool
    lancoop: bool
    offlinecoop: bool
    offlinecoopmax: int
    offlinemax: int
    onlinecoop: bool
    onlinecoopmax: int
    onlinemax: int
    platform: 'Platform'
    splitscreen: bool
    splitscreenonline: bool

class PlatformFamily(IgdbObject, total=False):
    name: str
    slug: str

class PlatformType(IgdbObject, total=False):
    name: str

class PlatformVersion(IgdbObject, total=False):
    name: str
    slug: str

class Platform(IgdbObject, total=False):
    abbreviation: str
    alternative_name: str
    generation: int
    name: str
    platform_family: 'PlatformFamily'
    platform_type: 'PlatformType'
    slug: str
    summary: str
    versions: Sequence['PlatformVersion']

class PlayerPerspective(IgdbObject, total=False):
    name: str
    slug: str

class DateFormat(IgdbObject, total=False):
    format: str

class ReleaseDateRegion(IgdbObject, total=False):
    region: str

class ReleaseDateStatus(IgdbObject, total=False):
    description: str
    name: str

class ReleaseDate(IgdbObject, total=False):
    date: int # TODO: Parse with date.fromtimestamp()
    date_format: 'DateFormat'
    human: str
    m: Literal[1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12] # Month (1-12)
    platform: Platform
    release_region: 'ReleaseDateRegion'
    status: 'ReleaseDateStatus'
    y: int

class Theme(IgdbObject, total=False):
    name: str
    slug: str

class Game(IgdbObject, total=False):
    age_ratings: Sequence[AgeRating]
    aggregated_rating: float
    aggregated_rating_count: int
    alternative_names: Sequence[AlternativeName]
    #artworks: Sequence[IgdbId | IgdbObject]
    bundles: Sequence['Game'] # name, ID, and platform
    collections: Sequence['Game'] # name, ID, and platform
    #cover: IgdbId | IgdbObject
    #created_at: datetime
    dlcs: Sequence['Game'] # name, ID, and platform
    expanded_games: Sequence['Game'] # name, ID, and platform
    expansions: Sequence['Game'] # name, ID, and platform
    #external_games: Sequence['ExternalGame']
    first_release_date: int # TODO: Parse with date.fromtimestamp()
    forks: Sequence['Game'] # name, ID, and platform
    franchise: Franchise
    franchises: Sequence[Franchise]
    game_engines: Sequence[GameEngine]
    game_localizations: Sequence[GameLocalization]
    game_modes: Sequence[GameMode]
    game_status: GameStatus
    game_type: GameType
    genres: Sequence[Genre]
    involved_companies: Sequence[InvolvedCompany]
    keywords: Sequence[Keyword]
    language_supports: Sequence[LanguageSupport]
    multiplayer_modes: Sequence[MultiplayerMode]
    name: str
    parent_game: 'Game'
    platforms: Sequence[Platform]
    player_perspectives: Sequence[PlayerPerspective]
    ports: Sequence['Game'] # name, ID, and platform
    rating: float
    rating_count: int
    release_dates: Sequence[ReleaseDate]
    remakes: Sequence['Game'] # name, ID, and platform
    remasters: Sequence['Game'] # name, ID, and platform
    #screenshots: Sequence['Screenshot']
    #similar_games: Sequence['Game'] # name, ID, and platform
    slug: str
    standalone_expansions: Sequence['Game'] # name, ID, and platform
    storyline: str
    summary: str
    tags: Sequence[int]
    themes: Sequence['Theme']
    total_rating: float
    total_rating_count: int
    url: str # TODO: Parse with urllib
    version_parent: 'Game' # name, ID, and platform
    version_title: str
    #videos: Sequence['Video']
    #websites: Sequence['Website']

SortDirection = Literal['asc', 'desc']
DEFAULT_SORT: tuple[str, SortDirection] = ('name', 'asc')

@dataclass(kw_only=True, eq=True)
class Query:
    fields: tuple[str, ...] | None
    exclude: tuple[str, ...] | None
    where: str | None
    limit: int | None
    offset: int | None
    sort: tuple[str, SortDirection] | None
    search: str | None

    def __init__(
            self,
            *, # Force keyword arguments for clarity
            fields: Iterable[str] | str | None = "*",
            exclude: Iterable[str] | str | None = None,
            where: Optional[str] = None,
            limit: Optional[int] = None,
            offset: Optional[int] = None,
            sort: tuple[str, SortDirection] | None = None,
            search: Optional[str] = None,
    ):
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
    title: str
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
    The name of zero or more directories within a Hasheous dump.
    Can have slashes for subdirectories (e.g. "Commodore Plus/4")
    '''

    def __init__(
            self,
            title: str,
            hasheous: str | Iterable[str] | None = None,
            alts: str | Iterable[str] | None = None,
            *, # Force keyword arguments for clarity
            fields: Iterable[str] | str | None = DEFAULT_GAME_FIELD_TUPLE,
            exclude: Iterable[str] | str | None = None,
            where: Optional[str] = None,
            limit: Optional[int] = 500,
            offset: Optional[int] = 0,
            sort: tuple[str, SortDirection] | None = DEFAULT_SORT,
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

def read_playlists(path: str) -> tuple[Playlist, ...]:
    class TomlPlaylistEntry(TypedDict):
        title: str
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
        return tuple(Playlist(**p) for p in playlist_objects)


dirname = os.path.dirname(__file__)
TOML_PATH = os.path.normpath(os.path.join(os.path.dirname(__file__), '..', 'metadat', 'igdb', 'igdb.toml'))

PLAYLISTS = read_playlists(TOML_PATH)

_idtranstable = str.maketrans({'-': None, ' ': None, '_': None, '.': None, '\'': None, '"': None})

PLAYLISTS_BY_TITLE = {p.title: p for p in PLAYLISTS}
PLAYLISTS_BY_TITLE_LOWER = {p.title.lower(): p for p in PLAYLISTS}
PLAYLISTS_BY_NORMALIZED_TITLE = {t.translate(_idtranstable):p for (t, p) in PLAYLISTS_BY_TITLE_LOWER.items()}
PLAYLISTS_BY_ANY: Mapping[str, Playlist] = ChainMap(
    PLAYLISTS_BY_TITLE,
    PLAYLISTS_BY_TITLE_LOWER,
    PLAYLISTS_BY_NORMALIZED_TITLE,
)

def get_playlist(identifier: str) -> Optional[Playlist]:
    """
    Get a playlist by its title or system ID, normalizing the identifier to lowercase and removing special characters.

    :param identifier: The title or system ID of the playlist to search for.
    :return: The Playlist object if found, otherwise None.
    """

    normalized_identifier = identifier.lower().translate(_idtranstable)
    return PLAYLISTS_BY_ANY.get(normalized_identifier, None)


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

__all__ = [
    "AgeRatingOrganization",
    "AgeRating",
    "AlternativeName",
    "Franchise",
    "GameEngine",
    "GameLocalization",
    "GameMode",
    "GameStatus",
    "GameType",
    "Genre",
    "CompanyStatus",
    "Company",
    "InvolvedCompany",
    "Region",
    "Keyword",
    "Language",
    "LanguageSupportType",
    "LanguageSupport",
    "MultiplayerMode",
    "PlatformFamily",
    "PlatformType",
    "PlatformVersion",
    "Platform",
    "PlayerPerspective",
    "DateFormat",
    "ReleaseDateRegion",
    "ReleaseDateStatus",
    "ReleaseDate",
    "Theme",
    "Game",
    "SortDirection",
    "Query",
    "Playlist",
    "Multiquery",
    "get_playlist",
    "get_by_title",
    "PLAYLISTS",
    "MAX_ACTIVE_QUERIES",
    "MAX_QUERY_RATE",
    "MAX_QUERY_PERIOD",
    "MULTIQUERY_MAX",
    "PLAYLISTS_BY_TITLE",
]