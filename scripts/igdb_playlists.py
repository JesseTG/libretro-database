from dataclasses import dataclass
from typing import Optional, NewType, Literal, TypedDict, Required
from collections.abc import Sequence, Iterable, Iterator

DEFAULT_GAME_FIELD_TUPLE: tuple[str, ...] = (
    "name",
    "age_ratings.organization.name",
    "age_ratings.rating_category.rating",
    "age_ratings.rating_content_descriptions.description",
    "aggregated_rating",
    "aggregated_rating_count",
    "alternative_names.name",
    "alternative_names.comment",
    "bundles.name",
    "collections.name",
    "collections.type.name",
    "first_release_date",
    "forks.name",
    "franchise.name",
    "franchises.name",
    "game_engines.name",
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
    "player_perspectives.name",
    "ports.name",
    "release_dates.date",
    "release_dates.human",
    "release_dates.m",
    "release_dates.y",
    "release_dates.date_format.format",
    "release_dates.release_region.region",
    "release_dates.status.description",
    "release_dates.status.name",
    "remakes.name",
    "remasters.name",
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

SortDirection = Literal['asc', 'desc']
DEFAULT_SORT = ('name', 'asc')

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
            fields: Iterable[str] | str | None = DEFAULT_GAME_FIELD_TUPLE,
            exclude: Iterable[str] | str | None = None,
            where: Optional[str] = None,
            limit: Optional[int] = 500,
            offset: Optional[int] = 0,
            sort: tuple[str, SortDirection] | None = DEFAULT_SORT,
            search: Optional[str] = None,
    ):
        # TODO: Default to the standard game fields if no fields are specified
        match fields:
            case str():
                self.fields = tuple(f.strip() for f in fields.split(","))
            case Iterable():
                self.fields = tuple(f.strip() for f in fields)
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

    def __str__(self):
        clauses: list[str] = []
        if self.fields:
            clauses.append(f"fields {','.join(self.fields)}")

        if self.exclude:
            clauses.append(f"exclude {','.join(self.exclude)}")

        if self.where:
            clauses.append(f"where {self.where}")

        if self.limit is not None:
            clauses.append(f"limit {self.limit}")

        if self.offset is not None:
            clauses.append(f"offset {self.offset}")

        if self.sort:
            clauses.append(f"sort {self.sort[0]} {self.sort[1]}")

        if self.search:
            clauses.append(f"search \"{self.search}\"")

        return '; '.join(clauses) + ';'

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

    query: Query

# NOTE: The "= (number)" syntax means "includes this value and possibly others"
PLAYLISTS = (
    Playlist("Amstrad - CPC", Query(where="platforms = (25)")),
    Playlist("Amstrad - GX4000", Query(where="platforms = (506)")),
    Playlist("Arduboy Inc - Arduboy", Query(where="platforms = (438)")),
    Playlist("Atari - Jaguar", Query(where="platforms = (62)")),
    Playlist("Atari - Lynx", Query(where="platforms = (61)")),
    Playlist("Atari - ST", Query(where="platforms = (63)")),
    Playlist("Atari - 2600", Query(where="platforms = (59)")),
    Playlist("Atari - 5200", Query(where="platforms = (66)")),
    Playlist("Atari - 7800", Query(where="platforms = (60)")),
    Playlist("Atari - 8-bit", Query(where="platforms = (65)")),
    Playlist(
        title="Atomiswave",
        platform_id=PlatformId(52),  # Arcade games
        platform_version_id=PlatformVersionId(652),
    ),
    Playlist("Bandai - WonderSwan", Query(where="platforms = (57)")),
    Playlist("Bandai - WonderSwan Color", Query(where="platforms = (123)")),
    # TODO: "Cannonball" playlist
    Playlist("Casio - Loopy", Query(where="platforms = (380)")),
    # IGDB lacks a `platforms` entry for the Casio PV-1000
    # TODO: "Cave Story" playlist
    # TODO: "ChaiLove" playlist
    # TODO: "CHIP-8" playlist
    Playlist("Coleco - ColecoVision", Query(where="platforms = (68)")),
    Playlist("Commodore - Amiga", Query(where="platforms = (16)")),
    Playlist("Commodore - CD32", Query(where="platforms = (114)")),
    Playlist("Commodore - CDTV", Query(where="platforms = (158)")),
    Playlist("Commodore - PET", Query(where="platforms = (90)")),
    Playlist("Commodore - Plus-4", Query(where="platforms = (94)")),
    Playlist("Commodore - VIC-20", Query(where="platforms = (71)")),
    Playlist("Commodore - 64", Query(where="platforms = (15)")),
    # TODO: "DICE" playlist
    # TODO: "Dinothawr" playlist
    # TODO: "DOOM" playlist
    Playlist("DOS", Query(where="platforms = (13)")),
    Playlist("Emerson - Arcadia 2001", Query(where="platforms = (473)")),
    # TODO: "Entex - Adventure Vision" playlist
    # TODO: "Enterprise - 128" playlist
    Playlist("Epoch - Super Cassette Vision", Query(where="platforms = (376)")),
    Playlist("Fairchild - Channel F", Query(where="platforms = (127)")),
    Playlist(
        title="FBNeo - Arcade Games",
        platform_id=(PlatformId(52), PlatformId(79), PlatformId(80)),
        # Arcade games, Neo Geo MVS, Neo Geo AES
    ),
    # TODO: "Flashback" playlist
    Playlist("Funtech - Super Acan", Query(where="platforms = (480)")),
    # TODO: "GamePark - GP32" playlist
    Playlist("GCE - Vectrex", Query(where="platforms = (70)")),
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

    Playlist("LeapFrog - Leapster Learning Game System", Query(where="platforms = (412)")),
    Playlist(
        title="LowRes NX",
        engine_id=EngineId(1672),
    ),
    Playlist("Magnavox - Odyssey2", Query(where="platforms = (133)")),
    Playlist("Mattel - Intellivision", Query(where="platforms = (67)")),
    Playlist("Microsoft - MSX", Query(where="platforms = (27)")),
    Playlist("Microsoft - MSX2", Query(where="platforms = (53)")),
    Playlist("Microsoft - Xbox", Query(where="platforms = (11)")),
    Playlist(
        title="MicroW8",
        engine_id=EngineId(1671),
    ),
    # TODO: "MrBoom" playlist
    Playlist(
        title="Mobile - J2ME",
        engine_id=EngineId(590)
    ),
    Playlist("NEC - PC Engine - TurboGrafx 16", Query(where="platforms = (86)")),
    Playlist("NEC - PC Engine CD - TurboGrafx-CD", Query(where="platforms = (150)")),
    Playlist("NEC - PC Engine SuperGrafx", Query(where="platforms = (128)")),
    Playlist("NEC - PC-8001 - PC-8801", Query(where="platforms = (125)")),
    Playlist("NEC - PC-98", Query(where="platforms = (149)")),
    Playlist("NEC - PC-FX", Query(where="platforms = (274)")),
    Playlist("Nintendo - e-Reader", Query(where="platforms = (510)")),
    Playlist("Nintendo - Family Computer Disk System", Query(where="platforms = (51)")),
    Playlist("Nintendo - Game Boy", Query(where="platforms = (33)")),
    Playlist("Nintendo - Game Boy Advance", Query(where="platforms = (24)")),
    Playlist("Nintendo - Game Boy Color", Query(where="platforms = (22)")),
    Playlist("Nintendo - GameCube", Query(where="platforms = (21)")),
    Playlist("Nintendo - Nintendo DS", Query(where="platforms = (20)")),
    Playlist("Nintendo - Nintendo DSi", Query(where="platforms = (159)")),

    # NES or Famicom
    Playlist("Nintendo - Nintendo Entertainment System", Query(where="platforms = (18, 99)")),

    # Nintendo 3DS or New Nintendo 3DS
    Playlist("Nintendo - Nintendo 3DS", Query(where="platforms = (37, 137)")),

    Playlist("Nintendo - Nintendo 64", Query(where="platforms = (4)")),
    Playlist("Nintendo - Nintendo 64DD", Query(where="platforms = (416)")),
    Playlist("Nintendo - Pokemon Mini", Query(where="platforms = (166)")),
    Playlist("Nintendo - Satellaview", Query(where="platforms = (306)")),

    # SNES or Super Famicom,
    # and has the "sufami turbo" keyword (and possibly others)
    # or its summary contains "sufami turbo" (any case) in the middle.
    # (IGDB has separate platform entries for the SNES (19) and Super Famicom (58))
    Playlist("Nintendo - Sufami Turbo", Query(where='platforms = (19, 58) & (keywords = (29241) | summary ~ *"sufami turbo"*)')),

    # SNES or Super Famicom, and does not have the "sufami turbo" keyword
    Playlist("Nintendo - Super Nintendo Entertainment System", Query(where='platforms = (19, 58) & keywords != (29241)')),
    Playlist("Nintendo - Virtual Boy", Query(where="platforms = (87)")),
    Playlist(
        "Nintendo - Wii",
        platform_id=PlatformId(5),
    ),
    # TODO: "Nintendo - Wii (Digital)" playlist
    Playlist("Nintendo - Wii U", Query(where="platforms = (41)")),
    Playlist("Philips - CD-i", Query(where="platforms = (117)")),
    # Playlist("Philips - Videopac+", Query(where="platforms = (133)")),
    # TODO: How to identify Videopac+ games? They're counted as Odyssey2 games in IGDB.
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
    Playlist("Sega - Dreamcast", Query(where="platforms = (23)")),
    Playlist("Sega - Game Gear", Query(where="platforms = (35)")),
    Playlist("Sega - Master System - Mark III", Query(where="platforms = (64)")),
    Playlist("Sega - Mega Drive - Genesis", Query(where="platforms = (29)")),
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
    Playlist("Sega - PICO", Query(where="platforms = (339)")),
    Playlist("Sega - Saturn", Query(where="platforms = (32)")),
    Playlist(
        title="Sega - ST-V",
        engine_id=EngineId(1780),
        # TODO: Get the PlatformId
    ),
    Playlist("Sega - SG-1000", Query(where="platforms = (84)")),
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
    Playlist("Sharp - X1", Query(where="platforms = (77)")),
    Playlist("Sharp - X68000", Query(where="platforms = (121)")),
    Playlist("Sinclair - ZX 81", Query(where="platforms = (373)")),
    Playlist("Sinclair - ZX Spectrum", Query(where="platforms = (26)")),
    # TODO: "Sinclair - ZX Spectrum +3" playlist
    Playlist(
        title="SNK - Neo Geo",
        platform_id=(PlatformId(79), PlatformId(80)),
        # Neo Geo MVS, Neo Geo AES
    ),
    Playlist("SNK - Neo Geo Pocket", Query(where="platforms = (119)")),
    Playlist("SNK - Neo Geo Pocket Color", Query(where="platforms = (120)")),
    Playlist("SNK - Neo Geo CD", Query(where="platforms = (136)")),
    Playlist("Sony - PlayStation", Query(where="platforms = (7)")),
    Playlist("Sony - PlayStation Portable", Query(where="platforms = (38)")),
    Playlist("Sony - PlayStation Portable (PSN)", Query(where="platforms = (38)")),
    Playlist("Sony - PlayStation Vita", Query(where="platforms = (46)")),
    Playlist("Sony - PlayStation 2", Query(where="platforms = (8)")),
    Playlist("Sony - PlayStation 3", Query(where="platforms = (9)")),
    # TODO: "Sony - PlayStation 3 (PSN)" playlist
    # TODO: "Spectravideo - SVI-318 - SVI-328" playlist
    Playlist("The 3DO Company - 3DO", Query(where="platforms = (50)")),
    Playlist(
        title="TIC-80",
        engine_id=EngineId(975),
    ),
    # TODO: "Thomson - MOTO" playlist
    Playlist("Tiger - Game.com", Query(where="platforms = (379)")),
    Playlist(
        title="Tomb Raider",
        engine_id=EngineId(1551),
        # TODO: Get the game IDs
    ),
    # TODO: "VTech - CreatiVision" playlist
    Playlist("VTech - V.Smile", Query(where="platforms = (439)")),
    Playlist("Uzebox", Query(where="platforms = (504)")),
    Playlist(
        title="Vircon32",
        engine_id=EngineId(1632)
    ),
    Playlist(
        title="WASM-4",
        engine_id=EngineId(1556),
    ),
    Playlist("Watara - Supervision", Query(where="platforms = (415)")),
    Playlist(
        title="Wolfenstein 3D",
        engine_id=EngineId(1636),
        # TODO: Get more game IDs, and get the engine ID for the original Wolfenstein 3D (1992) game
    )
)
# TODO: Look at what games are in platform IDs 409 (Legacy Computer), 112 (Microcomputer), 47 (Virtual Console)
# TODO: Add platforms to the MAME playlists
