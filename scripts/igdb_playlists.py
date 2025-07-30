from dataclasses import dataclass
from typing import Optional, NewType, Literal, TypedDict, Required
from collections.abc import Sequence, Iterable, Iterator


class QueryArgs(TypedDict, total=False):
    endpoint: Required[str]
    fields: Sequence[str]
    exclude: Sequence[str]
    where: str
    limit: int
    offset: int
    sort: tuple[str, Literal['asc', 'desc']]
    search: str
    looping: bool


@dataclass(kw_only=True)
class Query:
    endpoint: str
    fields: Optional[tuple[str]]
    exclude: Optional[tuple[str]]
    where: Optional[str]
    limit: Optional[int]
    offset: Optional[int]
    sort: Optional[tuple[str, Literal['asc', 'desc']]]
    search: Optional[str]
    looping: bool

    def __init__(self, kwargs: QueryArgs):
        self.endpoint = kwargs["endpoint"]
        self.fields = tuple(kwargs["fields"]) if "fields" in kwargs else None
        self.exclude = tuple(kwargs["exclude"]) if "exclude" in kwargs else None
        self.where = kwargs.get("where", None)
        self.limit = kwargs.get("limit", None)
        self.offset = kwargs.get("offset", None)
        self.sort = kwargs.get("sort", None)
        self.search = kwargs.get("search", None)
        self.looping = kwargs.get("looping", False)


@dataclass
class MultiqueryEntry:
    endpoint: str
    name: str
    query: Query


@dataclass
class Multiquery:
    queries: Sequence[MultiqueryEntry]

    def __init__(self, queries: Sequence[MultiqueryEntry]):
        self.queries = tuple(queries)

PlatformId = NewType("PlatformId", int)
PlatformVersionId = NewType("PlatformVersionId", int)
EngineId = NewType("EngineId", int)
PlaylistQuery = str | Query

@dataclass
class Playlist:
    title: str
    '''
    The title of the playlist,
    which is used as the filename for the playlist file.
    Usually follows the format "Manufacturer - Platform Name".
    '''

    # TODO: List the queries that should be used to generate the playlist.
    queries: str = Sequence[str]

    def __init__(self, title: str, *queries: Query):
        self.title = title

Playlist("fgsfds", "france", "fffds")


DEFAULT_GAME_FIELD_TUPLE: tuple[str, ...] = (
    "name",
    "age_ratings.organization.name",
    "age_ratings.rating_category.rating",
    "age_ratings.rating_content_descriptions.description",
    "aggregated_rating",
    "aggregated_rating_count",
    "alternative_names.name",
    "alternative_names.comment",
    "first_release_date",
    "forks.name",
    "franchise.name",
    "franchises.name",
    "game_localizations.name",
    "game_localizations.region.name",
    "game_localizations.region.identifier",
    "game_localizations.region.category",
    "game_modes.name",
    "game_status.status",
    "game_type.type",
    "genres.name",
    "involved_companies.company.name",
    "involved_companies.company.country",
    "involved_companies.company.description",
    "involved_companies.company.status.name",
    "involved_companies.developer",
    "involved_companies.porting",
    "involved_companies.publisher",
    "involved_companies.supporting",
    "keywords.name",
    "language_supports.language.locale",
    "language_supports.language.name",
    "language_supports.language_support_type.name",
    "multiplayer_modes.campaigncoop",
    "multiplayer_modes.dropin",
    "multiplayer_modes.lancoop",
    "multiplayer_modes.lancoop",
    "multiplayer_modes.offlinecoop",
    "multiplayer_modes.offlinecoopmax",
    "multiplayer_modes.offlinemax",
    "multiplayer_modes.onlinecoop",
    "multiplayer_modes.onlinecoopmax",
    "multiplayer_modes.onlinemax",
    "multiplayer_modes.splitscreen",
    "multiplayer_modes.splitscreenonline",
    "platforms.name",
    "platforms.abbreviation",
    "platforms.alternative_name",
    "platforms.generation",
    "platforms.platform_family.name",
    "platforms.platform_type.name",
    "platforms.slug",
    "platforms.summary",
    "platforms.versions.name",
    "platforms.versions.connectivity",
    "platforms.versions.cpu",
    "player_perspectives.name",
    "ports",
    "release_dates.date",
    "release_dates.human",
    "release_dates.m",
    "release_dates.y",
    "release_dates.date_format.format",
    "release_dates.release_region.region",
    "release_dates.status.description",
    "release_dates.status.name",
    "remakes",
    "remasters",
    "similar_games",
    "slug",
    "standalone_expansions",
    "dlcs",
    "expanded_games",
    "expansions",
    "external_games",
    "storyline",
    "summary",
    "tags",
    "themes.name",
    "version_title",
    "websites.url",
    "websites.type.type",
)
DEFAULT_GAME_FIELDS = ''.join(DEFAULT_GAME_FIELD_TUPLE)

# TODO: Need to specify what the queries for each playlist should be.
#  Use a higher-order function so I can generate the queries based on the number of entries
#  (since there's a limit of 1000 entries per query).

PLAYLISTS = (
    Playlist(
        title="Amstrad - CPC",
        platform_id=PlatformId(25),
    ),
    Playlist(
        title="Amstrad - GX4000",
        platform_id=PlatformId(506),
    ),
    Playlist(
        title="Arduboy Inc - Arduboy",
        platform_id=PlatformId(438),
    ),
    Playlist(
        title="Atari - Jaguar",
        platform_id=PlatformId(62),
    ),
    Playlist(
        title="Atari - Lynx",
        platform_id=PlatformId(61),
    ),
    Playlist(
        title="Atari - ST",
        platform_id=PlatformId(63),
    ),
    Playlist(
        title="Atari - 2600",
        platform_id=PlatformId(59),
    ),
    Playlist(
        title="Atari - 5200",
        platform_id=PlatformId(66),
    ),
    Playlist(
        title="Atari - 7800",
        platform_id=PlatformId(60),
    ),
    Playlist(
        title="Atari - 8-bit",
        platform_id=PlatformId(65),
    ),
    Playlist(
        title="Atomiswave",
        platform_id=PlatformId(52), # Arcade games
        platform_version_id=PlatformVersionId(652),
    ),
    Playlist(
        title="Bandai - WonderSwan",
        platform_id=PlatformId(57),
    ),
    Playlist(
        title="Bandai - WonderSwan Color",
        platform_id=PlatformId(123),
    ),
    # TODO: "Cannonball" playlist
    Playlist(
        title="Casio - Loopy",
        platform_id=PlatformId(380),
    ),
    # IGDB lacks a `platforms` entry for the Casio PV-1000
    # TODO: "Cave Story" playlist
    # TODO: "ChaiLove" playlist
    # TODO: "CHIP-8" playlist
    Playlist(
        title="Coleco - ColecoVision",
        platform_id=PlatformId(68),
    ),
    Playlist(
        title="Commodore - Amiga",
        platform_id=PlatformId(16),
    ),
    Playlist(
        title="Commodore - CD32",
        platform_id=PlatformId(114),
    ),
    Playlist(
        title="Commodore - CDTV",
        platform_id=PlatformId(158),
    ),
    Playlist(
        title="Commodore - PET",
        platform_id=PlatformId(90),
    ),
    Playlist(
        title="Commodore - Plus-4",
        platform_id=PlatformId(94),
    ),
    Playlist(
        title="Commodore - VIC-20",
        platform_id=PlatformId(71),
    ),
    Playlist(
        title="Commodore - 64",
        platform_id=PlatformId(15),
    ),
    # TODO: "DICE" playlist
    # TODO: "Dinothawr" playlist
    # TODO: "DOOM" playlist
    Playlist(
        title="DOS",
        platform_id=PlatformId(13),
    ),
    Playlist(
        title="Emerson - Arcadia 2001",
        platform_id=PlatformId(473),
    ),
    # TODO: "Entex - Adventure Vision" playlist
    # TODO: "Enterprise - 128" playlist
    Playlist(
        title="Epoch - Super Cassette Vision",
        platform_id=PlatformId(376),
    ),
    Playlist(
        title="Fairchild - Channel F",
        platform_id=PlatformId(127),
    ),
    Playlist(
        title="FBNeo - Arcade Games",
        platform_id=(PlatformId(52), PlatformId(79), PlatformId(80)),
        # Arcade games, Neo Geo MVS, Neo Geo AES
    ),
    # TODO: "Flashback" playlist
    # TODO: "Funtech - Super Acan" playlist
    # TODO: "GamePark - GP32" playlist
    Playlist(
        title="GCE - Vectrex",
        platform_id=PlatformId(70),
    ),
    Playlist(
        title="Handheld Electronic Game",
        platform_id=(PlatformId(307), PlatformId(411)),
        # Game & Watch, Handheld LCD games
    ),
    Playlist(
        title="Infocom - Z-Machine",
        engine_id=EngineId(71),
    ),
    # TODO: "Hartung - Game Master" playlist
    # TODO: all the MAME playlists
    # TODO: "Jump 'n Bump" playlist

    Playlist(
        title="LeapFrog - Leapster Learning Game System",
        platform_id=PlatformId(412),
    ),
    Playlist(
        title="LowRes NX",
        engine_id=EngineId(1672),
    ),
    Playlist(
        title="Magnavox - Odyssey2",
        platform_id=PlatformId(133),
    ),
    Playlist(
        title="Mattel - Intellivision",
        platform_id=PlatformId(67),
    ),
    Playlist(
        title="Microsoft - MSX",
        platform_id=PlatformId(27),
    ),
    Playlist(
        title="Microsoft - MSX2",
        platform_id=PlatformId(53),
    ),
    Playlist(
        title="Microsoft - Xbox",
        platform_id=PlatformId(11),
    ),
    Playlist(
        title="MicroW8",
        engine_id=EngineId(1671),
    ),
    # TODO: "MrBoom" playlist
    Playlist(
        title="Mobile - J2ME",
        engine_id=EngineId(590)
    ),
    Playlist(
        title="NEC - PC Engine - TurboGrafx 16",
        platform_id=PlatformId(86),
    ),
    Playlist(
        title="NEC - PC Engine CD - TurboGrafx-CD",
        platform_id=PlatformId(150),
    ),
    Playlist(
        title="NEC - PC Engine SuperGrafx",
        platform_id=PlatformId(128),
    ),
    Playlist(
        title="NEC - PC-8001 - PC-8801",
        platform_id=PlatformId(125),
    ),
    Playlist(
        title="NEC - PC-98",
        platform_id=PlatformId(149),
    ),
    Playlist(
        title="NEC - PC-FX",
        platform_id=PlatformId(274),
    ),
    Playlist(
        title="Nintendo - e-Reader",
        platform_id=PlatformId(510),
    ),
    Playlist(
        title="Nintendo - Family Computer Disk System",
        platform_id=PlatformId(51),
    ),
    Playlist(
        title="Nintendo - Game Boy",
        platform_id=PlatformId(33),
    ),
    Playlist(
        title="Nintendo - Game Boy Advance",
        platform_id=PlatformId(24),
    ),
    Playlist(
        title="Nintendo - Game Boy Color",
        platform_id=PlatformId(22),
    ),
    Playlist(
        title="Nintendo - GameCube",
        platform_id=PlatformId(21),
    ),
    Playlist(
        title="Nintendo - Nintendo DS",
        platform_id=PlatformId(20),
    ),
    Playlist(
        title="Nintendo - Nintendo DSi",
        platform_id=PlatformId(159),
    ),
    Playlist(
        title="Nintendo - Nintendo Entertainment System",
        platform_id=(PlatformId(99), PlatformId(18)),
        # Famicom, NES
    ),
    Playlist(
        title="Nintendo - Nintendo 3DS",
        platform_id=(PlatformId(137), PlatformId(37),),
        # New Nintendo 3DS, Nintendo 3DS
    ),
    Playlist(
        title="Nintendo - Nintendo 64",
        platform_id=PlatformId(4),
    ),
    Playlist(
        title="Nintendo - Nintendo 64DD",
        platform_id=PlatformId(416),
    ),
    Playlist(
        title="Nintendo - Pokemon Mini",
        platform_id=PlatformId(166),
    ),
    Playlist(
        title="Nintendo - Satellaview",
        platform_id=PlatformId(306),
    ),
    # TODO: "Nintendo - Sufami Turbo" playlist
    Playlist(
        title="Nintendo - Super Nintendo Entertainment System",
        platform_id=(PlatformId(58), PlatformId(19)),
        # Super Famicom, SNES
    ),
    Playlist(
        "Nintendo - Virtual Boy",
        platform_id=PlatformId(87)
    ),
    Playlist(
        title="Nintendo - Wii",
        platform_id=PlatformId(5),
    ),
    # TODO: "Nintendo - Wii (Digital)" playlist
    Playlist(
        title="Nintendo - Wii U",
        platform_id=PlatformId(41),
    ),
    Playlist(
        title="Philips - CD-i",
        platform_id=PlatformId(117),
    ),
    # TODO: "Philips - Videopac+" playlist
    Playlist(
        title="PICO-8",
        engine_id=EngineId(829),
    ),
    Playlist(
        title="PuzzleScript",
        engine_id=EngineId(831),
    ),
    # TODO: "Quake" playlist
    # TODO: "Quake II" playlist
    # TODO: "Quake III" playlist
    # TODO: "RCA - Studio II" playlist
    # TODO: "Rick Dangerous" playlist
    Playlist(
        title="RPG Maker",
        engine_id=(EngineId(696), EngineId(765)),
        # RPG Maker 2000, RPG Maker 2003
    ),
    Playlist(
        title="ScummVM",
        engine_id=EngineId(53),
    ),
    Playlist(
        title="Sega - Dreamcast",
        platform_id=PlatformId(23),
    ),
    Playlist(
        title="Sega - Game Gear",
        platform_id=PlatformId(35),
    ),
    Playlist(
        title="Sega - Master System - Mark III",
        platform_id=PlatformId(64),
    ),
    Playlist(
        title="Sega - Mega Drive - Genesis",
        platform_id=PlatformId(29),
    ),
    Playlist(
        title="Sega - Mega-CD - Sega CD",
        platform_id=(PlatformId(78), PlatformId(482)),
        # Sega CD, Sega Mega-CD 32X
    ),
    Playlist(
        title="Sega - Naomi",
        platform_id=PlatformId(52),
        platform_version_id=PlatformVersionId(637),
    ),
    Playlist(
        title="Sega - PICO",
        platform_id=PlatformId(339),
    ),
    Playlist(
        title="Sega - Saturn",
        platform_id=PlatformId(32),
    ),
    Playlist(
        title="Sega - ST-V",
        engine_id=EngineId(1780),
        # TODO: Get the PlatformId
    ),
    Playlist(
        title="Sega - SG-1000",
        platform_id=PlatformId(84),
    ),
    Playlist(
        title="Sega - 32X",
        platform_id=(PlatformId(30), PlatformId(482)),
        # Sega 32X, Sega Mega-CD 32X
    ),
    Playlist(
        title="Sega - Naomi 2",
        platform_id=PlatformId(52),
        platform_version_id=PlatformVersionId(651),
    ),
    Playlist(
        title="Sharp - X1",
        platform_id=PlatformId(77),
    ),
    Playlist(
        title="Sharp - X68000",
        platform_id=PlatformId(121),
    ),
    Playlist(
        title="Sinclair - ZX 81",
        platform_id=PlatformId(373),
    ),
    Playlist(
        title="Sinclair - ZX Spectrum",
        platform_id=PlatformId(26),
    ),
    # TODO: "Sinclair - ZX Spectrum +3" playlist
    Playlist(
        title="SNK - Neo Geo",
        platform_id=(PlatformId(79), PlatformId(80)),
        # Neo Geo MVS, Neo Geo AES
    ),
    Playlist(
        title="SNK - Neo Geo Pocket",
        platform_id=PlatformId(119),
    ),
    Playlist(
        title="SNK - Neo Geo Pocket Color",
        platform_id=PlatformId(120),
    ),
    Playlist(
        title="SNK - Neo Geo CD",
        platform_id=PlatformId(136),
    ),
    Playlist(
        title="Sony - PlayStation",
        platform_id=PlatformId(7),
    ),
    Playlist(
        title="Sony - PlayStation Portable",
        platform_id=PlatformId(38),
    ),
    Playlist(
        title="Sony - PlayStation Portable (PSN)",
        platform_id=PlatformId(38),
    ),
    Playlist(
        title="Sony - PlayStation Vita",
        platform_id=PlatformId(46),
    ),
    Playlist(
        title="Sony - PlayStation 2",
        platform_id=PlatformId(8),
    ),
    Playlist(
        title="Sony - PlayStation 3",
        platform_id=PlatformId(9),
    ),
    # TODO: "Sony - PlayStation 3 (PSN)" playlist
    # TODO: "Spectravideo - SVI-318 - SVI-328" playlist
    Playlist(
        title="The 3DO Company - 3DO",
        platform_id=PlatformId(50),
    ),
    Playlist(
        title="TIC-80",
        engine_id=EngineId(975),
    ),
    # TODO: "Thomson - MOTO" playlist
    Playlist(
        title="Tiger - Game.com",
        platform_id=PlatformId(379),
    ),
    Playlist(
        title="Tomb Raider",
        engine_id=EngineId(1551),
        # TODO: Get the game IDs
    ),
    # TODO: "VTech - CreatiVision" playlist
    Playlist(
        title="VTech - V.Smile",
        platform_id=PlatformId(439),
    ),
    Playlist(
        title="Uzebox",
        platform_id=PlatformId(504),
    ),
    Playlist(
        title="Vircon32",
        engine_id=EngineId(1632)
    ),
    Playlist(
        title="WASM-4",
        engine_id=EngineId(1556),
    ),
    Playlist(
        title="Watara - Supervision",
        platform_id=PlatformId(415),
    ),
    Playlist(
        title="Wolfenstein 3D",
        engine_id=EngineId(1636),
        # TODO: Get more game IDs, and get the engine ID for the original Wolfenstein 3D (1992) game
    )
)
# TODO: Look at what games are in platform IDs 409 (Legacy Computer), 112 (Microcomputer), 47 (Virtual Console)
# TODO: Add platforms to the MAME playlists