"""
Dictionary definitions taken from https://github.com/gaseous-project/hasheous/blob/main/hasheous-lib/Models/DataObjectItem.cs
"""

import argparse
import asyncio
import dataclasses
import itertools
import os
import pickle
import sys
import time
import tomllib
import zipfile

from collections.abc import Sequence, Mapping
from concurrent.futures import ProcessPoolExecutor
from dataclasses import field
from pathlib import Path
from pprint import pprint
from typing import Collection, Iterator, Literal, NamedTuple, Optional, TypeAlias, TypedDict, Union, cast
from zipfile import ZipFile

import aiofiles
import aiofiles.os
import backoff
import httpx
import typelib

METADATA_MAP_URL = "https://hasheous.org/api/v1/Dumps/MetadataMap.zip"


@dataclasses.dataclass(frozen=True, kw_only=True, slots=True)
class SignatureDataObject:
    SignatureId: Optional[str] = None
    Name: Optional[str] = None
    Year: Optional[str] = None
    Platform: Optional[str] = None
    SourceId: Optional[str] = None
    Publisher: Optional[str] = None
    MetadataSource: Optional[str] = None


MappingStatus: TypeAlias = Literal["NotMapped", "Mapped", "MappedWithErrors"]

MatchMethodType: TypeAlias = Literal[
    "NoMatch",
    "Automatic",
    "Manual",
    "AutomaticTooManyMatches",
    "ManualByAdmin",
    "Voted",
]

MetadataSource: TypeAlias = Literal[
    "None",
    "IGDB",
    "TheGamesDb",
    "RetroAchievements",
    "GiantBomb",
    "Steam",
    "GOG",
    "EpicGameStore",
    "Wikipedia",
    "SteamGridDb",
]

@dataclasses.dataclass(frozen=True, kw_only=True, slots=True)
class MetadataItem:
    Id: str
    ImmutableId: str
    Status: MappingStatus
    MatchMethod: MatchMethodType
    Source: MetadataSource
    Link: str
    LastSearch: str
    NextSearch: str
    WinningVoteCount: int
    TotalVoteCount: int
    WinningVotePercent: int

AttributeType: TypeAlias = Literal[
    "LongString",
    "ShortString",
    "DateTime",
    "ImageId",
    "ImageAttribution",
    "Link",
    "Boolean",
    "ObjectRelationship",
    "EmbeddedList",
]

AttributeName: TypeAlias = Literal[
    "Description",
    "Manufacturer",
    "Publisher",
    "Logo",
    "Platform",
    "Year",
    "Country",
    "Language",
    "ROMs",
    "VIMMManualId",
    "LogoAttribution",
    "VIMMPlatformName",
    "HomePage",
    "IssueTracker",
    "Screenshot1",
    "Screenshot2",
    "Screenshot3",
    "Screenshot4",
    "Wikipedia",
    "Public",
    "DumpFile",
]

DataObjectType: TypeAlias = Literal["None", "Company", "Platform", "Game", "ROM", "App"]
RomTypeName: TypeAlias = Literal["Unknown", "Disc", "Disk", "File", "Part", "Tape", "Side"]
SignatureSourceType: TypeAlias = Literal[
    "None",
    "TOSEC",
    "MAMEArcade",
    "MAMEMess",
    "NoIntro",
    "NoIntros",
    "Redump",
    "WHDLoad",
    "RetroAchievements",
    "FBNeo",
    "PureDOSDAT",
    "Pleasuredome",
    "Generic",
]

@dataclasses.dataclass(frozen=True, kw_only=True, slots=True)
class MediaType:
    MediaType: Optional[RomTypeName] = None
    Media: Optional[str] = None
    Number: Optional[int] = None
    Count: Optional[int] = None
    Side: Optional[str] = None

@dataclasses.dataclass(frozen=True, kw_only=True, slots=True)
class RomItem:
    Score: int
    Attributes: Optional[Mapping[str, str]] = None
    RomType: RomTypeName
    Id: Optional[str] = None
    Name: Optional[str] = None
    Size: Optional[int] = None
    Crc: Optional[str] = None
    Md5: Optional[str] = None
    Sha1: Optional[str] = None
    Sha256: Optional[str] = None
    Status: Optional[str] = None
    Country: Optional[Mapping[str, str]] = None
    Language: Optional[Mapping[str, str]] = None
    DevelopmentStatus: Optional[str] = None
    RomTypeMedia: Optional[str] = None
    MediaDetail: Optional[MediaType] = None
    MediaLabel: Optional[str] = None
    SignatureSource: Optional[SignatureSourceType] = None

AttributeValue: TypeAlias = Union["DataObject", Sequence[RomItem], str]

@dataclasses.dataclass(frozen=True, kw_only=True, slots=True)
class Attribute:
    attributeType: AttributeType
    '''Not a typo, the API serializes it this way'''

    attributeName: AttributeName
    '''Not a typo, the API serializes it this way'''

    attributeRelationType: DataObjectType
    '''Not a typo, the API serializes it this way'''

    Value: AttributeValue
    Id: Optional[int] = None


@dataclasses.dataclass(frozen=True, kw_only=True, slots=True)
class DataObject:
    Id: int
    ObjectType: DataObjectType
    SignatureDataObjects: Sequence[SignatureDataObject]
    Metadata: Sequence[MetadataItem]
    Attributes: Sequence[Attribute]
    CreatedDate: str
    UpdatedDate: str
    Name: str

    # Internal cache of some commonly used properties
    _Roms: tuple[RomItem, ...] = field(default=(), init=False, repr=False, compare=False)
    _IgdbId: Optional[int] = field(default=None, init=False, repr=False, compare=False)
    _Crcs: frozenset[str] = field(default=frozenset(), init=False, repr=False, compare=False)
    _Md5s: frozenset[str] = field(default=frozenset(), init=False, repr=False, compare=False)
    _Sha1s: frozenset[str] = field(default=frozenset(), init=False, repr=False, compare=False)

    def __post_init__(self):
        '''
        Called by the dataclass machinery after __init__,
        but before the instance is returned.
        '''
        if self.ObjectType == 'Game':
            for a in self.Attributes:
                if a.attributeName == 'ROMs' and isinstance(a.Value, Sequence) and not isinstance(a.Value, str):
                    object.__setattr__(self, '_Roms', tuple(a.Value))  # Bypass frozen restriction
                    break

            for m in self.Metadata:
                if m.Source == 'IGDB' and m.Status == 'Mapped':
                    object.__setattr__(self, '_IgdbId', int(m.ImmutableId))  # Bypass frozen restriction
                    break

            if self._Roms:
                crcs = frozenset(rom.Crc.lower() for rom in self._Roms if rom.Crc)
                md5s = frozenset(rom.Md5.lower() for rom in self._Roms if rom.Md5)
                sha1s = frozenset(rom.Sha1.lower() for rom in self._Roms if rom.Sha1)
                object.__setattr__(self, '_Crcs', crcs)  # Bypass frozen restriction
                object.__setattr__(self, '_Md5s', md5s)  # Bypass frozen restriction
                object.__setattr__(self, '_Sha1s', sha1s)  # Bypass frozen restriction


    def has_rom(self, crc: str | None, md5: str | None, sha1: str | None) -> bool:
        if crc and crc.lower() in self._Crcs:
            return True

        if md5 and md5.lower() in self._Md5s:
            return True

        if sha1 and sha1.lower() in self._Sha1s:
            return True

        return False

    @property
    def rom_list(self) -> Sequence[RomItem]:
        """Return the list of ROMs, or an empty tuple if none are present."""
        return self._Roms

    @property
    def igdb_id(self) -> int | None:
        return self._IgdbId

DataObjectCodec: typelib.Codec[DataObject] = typelib.codec(DataObject)

class HasheousIndex:
    def __init__(self, objects: Iterator[DataObject]) -> None:
        self.objects = tuple(objects)
        self.by_id: dict[int, DataObject] = {obj.Id: obj for obj in objects}
        self.by_name: dict[str, DataObject] = {obj.Name.lower(): obj for obj in objects if obj.Name}
        self.by_igdb_id: dict[int, DataObject] = {obj.igdb_id: obj for obj in objects if obj.igdb_id is not None}

        self.by_crc: dict[str, DataObject] = {}
        self.by_md5: dict[str, DataObject] = {}
        self.by_sha1: dict[str, DataObject] = {}

        for obj in objects:
            if obj.ObjectType != 'Game':
                continue

            for rom in obj.rom_list:
                if rom.Crc and rom.Crc.lower() not in self.by_crc:
                    self.by_crc[rom.Crc.lower()] = obj

                if rom.Md5 and rom.Md5.lower() not in self.by_md5:
                    self.by_md5[rom.Md5.lower()] = obj

                if rom.Sha1 and rom.Sha1.lower() not in self.by_sha1:
                    self.by_sha1[rom.Sha1.lower()] = obj

class HasheousZip(NamedTuple):
    name: str
    objects: Sequence[DataObject]

def parse_zip(path: Path) -> HasheousZip:
    start = time.perf_counter_ns()
    with ZipFile(path, 'r') as zip:
        paths = zip.infolist()
        json_infos = filter(lambda p: p.filename.endswith('.json') and p.filename != 'PlatformMapping.json', paths)
        objects = map(lambda i: DataObjectCodec.decode(zip.read(i)), json_infos)

        result = HasheousZip(path.stem, tuple(objects))

    end = time.perf_counter_ns()
    duration = (end - start) / 1_000_000
    print(f"Parsed {len(result.objects)} DataObjects from {path.name} in {duration:.2f} ms")
    return result
    # Returning the stem makes it easier to aggregate results later


async def load_dataobjects(metadata_dir: Path, hasheous_dirs: Mapping[str, Sequence[str]]) -> Mapping[str, Sequence[DataObject]]:
    """
    Load Hasheous DataObjects from the given metadata directory for the specified playlists.

    :param metadata_dir: Path to the directory containing Hasheous metadata ZIP files.
    :param hasheous_dirs: A mapping of playlist names to the directories
    in MetadataMap.zip to load DataObjects from.

    :return: A mapping of playlist names to the DataObjects representing
    the games in those playlists.
    """

    print("Loading Hasheous DataObjects from", metadata_dir)

    with ProcessPoolExecutor() as executor:
        hasheous_paths = {playlist: tuple(metadata_dir / f"{d}.zip" for d in dirs) for (playlist, dirs) in hasheous_dirs.items()}
        hasheous_zip_paths = frozenset(itertools.chain.from_iterable(v for v in hasheous_paths.values()))
        iterator = executor.map(parse_zip, hasheous_zip_paths)
        dataobjects = {p.name: p.objects for p in iterator}

    results: dict[str, Sequence[DataObject]] = {}
    for (name, zips) in hasheous_dirs.items():
        zip_objects = (dataobjects.get(z, ()) for z in zips if z in dataobjects)
        objects = itertools.chain.from_iterable(zip_objects)
        results[name] = tuple(objects)

    print(f"Loaded {sum(len(v) for v in results.values())} DataObjects for {len(results)} playlists")
    return results

def read_dump_names(path: str) -> Collection[str]:
    class TomlPlaylistEntry(TypedDict):
        hasheous: Sequence[str]

    with open(path, "rb") as playlist_file:
        toml = tomllib.load(playlist_file)

        if not (igdb := toml.get('igdb')):
            raise KeyError(f"Missing 'igdb' section in TOML file at {path}")

        if not (playlists := igdb.get('playlists')):
            raise KeyError(f"Missing 'playlists' array in 'igdb' table of TOML file at {path}")

        if not isinstance(playlists, list):
            raise TypeError(f"Expected 'playlists' to be a list; got {type(playlists).__name__}")

        playlist_objects = cast(Sequence[TomlPlaylistEntry], playlists)
        playlists_with_hasheous = filter(lambda p: 'hasheous' in p, playlist_objects)
        return frozenset(itertools.chain.from_iterable(p['hasheous'] for p in playlists_with_hasheous))


dirname = os.path.dirname(__file__)
TOML_PATH = os.path.normpath(os.path.join(os.path.dirname(__file__), '..', 'metadat', 'igdb', 'igdb.toml'))

def _on_backoff(details):
    print("Retrying after backoff:", details['target'].__name__, "with args:", details['args'], "and kwargs:", details['kwargs'], file=sys.stderr)

RETRY_CODES = (
    httpx.codes.REQUEST_TIMEOUT,
    httpx.codes.TOO_MANY_REQUESTS,
    httpx.codes.INTERNAL_SERVER_ERROR,
    httpx.codes.BAD_GATEWAY,
    httpx.codes.SERVICE_UNAVAILABLE,
    httpx.codes.GATEWAY_TIMEOUT,
)

HASHEOUS_BASE_URL = "https://hasheous.org/api/v1/Dumps/platforms/"

def _giveup(e: Exception):
    print("Exception raised during query:", e, file=sys.stderr)
    if not isinstance(e, httpx.HTTPStatusError):
        # Give up if query_endpoint failed with something besides HTTPStatusError
        return True

    if e.response.status_code in RETRY_CODES:
        # Don't give up on server errors (5xx), we might just be unlucky
        # or rate-limited (429), so we should back off and retry.
        return False

    return e.response.is_error

async def handle_fetch(args: argparse.Namespace) -> None:
    outdir: Path = args.outdir
    dumps: Collection[str] = sorted(args.dumps or read_dump_names(TOML_PATH))
    verbose = bool(args.verbose)

    if verbose:
        print(f"Output directory: {outdir}")
        pprint(dumps)

    await aiofiles.os.makedirs(outdir, exist_ok=True)

    async with asyncio.TaskGroup() as group:
        @backoff.on_exception(backoff.expo, httpx.HTTPStatusError, max_tries=5, giveup=_giveup, on_backoff=_on_backoff)
        async def fetch_dump(name: str):
            dump_url = f"{HASHEOUS_BASE_URL}{name}.zip"
            if verbose:
                print(f"Fetching {dump_url}")

            async with httpx.AsyncClient() as client:
                async with client.stream("GET", dump_url) as response:
                    response.raise_for_status()
                    content_type = response.headers.get('content-type')

                    if not content_type or 'application/zip' not in content_type.lower():
                        raise ValueError(f"Expected content type 'application/zip', got {content_type} for dump {name}")

                    outpath = outdir / f"{name}.zip"
                    async with aiofiles.open(outpath, "wb") as out_file:
                        async for chunk in response.aiter_bytes():
                            await out_file.write(chunk)

            if verbose:
                print(f"Saved dump to {outpath}")

        for d in dumps:
            group.create_task(fetch_dump(d), name="fetch_dump_" + d)

async def handle_index(args: argparse.Namespace) -> None:
    input_paths: Collection[str] = args.paths
    verbose = bool(args.verbose)
    output: Path = args.output

    if verbose:
        print("Input paths:", input_paths)

    zip_paths: set[Path] = set()
    for path in map(Path, input_paths):
        if zipfile.is_zipfile(path):
            zip_paths.add(path.resolve())
        elif path.is_dir():
            glob_paths = path.rglob('*.zip')
            glob_zips = filter(zipfile.is_zipfile, glob_paths)
            zip_paths.update(p.resolve() for p in glob_zips)

    if not zip_paths:
        print("No ZIP files found in the specified input paths.", file=sys.stderr)
        return

    if verbose:
        print(f"Found {len(zip_paths)} ZIP files to index.")
        pprint(zip_paths)

    output.parent.mkdir(parents=True, exist_ok=True)

    with ProcessPoolExecutor() as executor:
        iterator = executor.map(parse_zip, zip_paths)
        dataobjects = itertools.chain.from_iterable(p.objects for p in iterator)

        index = HasheousIndex(dataobjects)

    print(f"Loaded all DataObjects, indexing...")

    with open(output, "wb") as out_file:
        pickle.dump(index, out_file, protocol=5)

    print(f"Indexed {len(index.objects)} DataObjects from {len(zip_paths)} ZIP files, saved to {output}")

def main():
    """Main entry point for the script."""

    parser = argparse.ArgumentParser(
        description="Utilities for fetching and processing data from Hasheous.",
    )

    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Show more logging output"
    )

    subparsers = parser.add_subparsers(
        dest="command",
        help="Available commands",
        required=True
    )

    # fetch subcommand
    fetch_parser = subparsers.add_parser(
        "fetch",
        help="Fetch data from Hasheous and save it to the specified directory"
    )
    fetch_parser.add_argument(
        "--dumps",
        type=str,
        help="The names of the Hasheous dumps to fetch. Defaults to all 'hasheous' entries in metadat/igdb/igdb.toml",
        action="extend",
        nargs="*",
        default=None
    )
    fetch_parser.add_argument(
        "outdir",
        type=Path,
        help="The output directory for the scraped JSON files",
        default="tmp/hasheous",
    )
    fetch_parser.set_defaults(func=handle_fetch)

    # index subcommand
    index_parser = subparsers.add_parser(
        "index",
        help="Create an index of DataObjects from the specified ZIP files."
    )
    index_parser.add_argument(
        "paths",
        help="A ZIP file or a directory containing Hasheous ZIP files",
        default=["tmp/hasheous"],
        action="extend",
        nargs="*",
    )
    index_parser.add_argument(
        "--output",
        type=Path,
        help="The output file to save the index to.",
        default="tmp/index/hasheous_index.pickle",
    )
    index_parser.set_defaults(func=handle_index)


    # Parse arguments and call appropriate handler

    args = parser.parse_args()
    asyncio.run(args.func(args))

__all__ = [
    "DataObject",
    "DataObjectType",
    "SignatureDataObject",
    "MetadataItem",
    "Attribute",
    "RomItem",
    "METADATA_MAP_URL",
    "AttributeType",
    "AttributeName",
    "MappingStatus",
    "MatchMethodType",
    "MetadataSource",
    "RomTypeName",
    "SignatureSourceType",
    "load_dataobjects",
]

if __name__ == "__main__":
    main()
