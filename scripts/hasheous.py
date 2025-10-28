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

from collections.abc import Iterable, Sequence, Mapping
from collections import ChainMap
from concurrent.futures import Executor, ProcessPoolExecutor, ThreadPoolExecutor
from pathlib import Path
from pprint import pprint
from typing import Collection, Literal, NamedTuple, NewType, Optional, TypeAlias, TypedDict, Union, cast
from zipfile import ZipFile


import aiofiles
import aiofiles.os
import backoff
import httpx
import typelib

from igdb import PLAYLISTS, IgdbId, Playlist, PlaylistTitle

METADATA_MAP_URL = "https://hasheous.org/api/v1/Dumps/MetadataMap.zip"

HasheousId = NewType('HasheousId', int)


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
    Id: HasheousId
    ObjectType: DataObjectType
    SignatureDataObjects: Sequence[SignatureDataObject]
    Metadata: Sequence[MetadataItem]
    Attributes: Sequence[Attribute]
    CreatedDate: str
    UpdatedDate: str
    Name: str

DataObjectCodec: typelib.Codec[DataObject] = typelib.codec(DataObject)

class HasheousIndex:
    def __init__(self, games: Iterable[tuple[PlaylistTitle, Iterable[DataObject]]]) -> None:
        playlists_iterators = dict(games)
        playlists = {title: tuple(obj_iter) for title, obj_iter in playlists_iterators.items()}
        self.by_playlist = playlists
        self.by_id: dict[int, DataObject] = {}
        self.by_igdb_id: dict[IgdbId, DataObject] = {}
        self.hasheous_to_igdb: dict[HasheousId, IgdbId] = {}
        self.supports_achievements: set[HasheousId] = set()

        self.by_crc: dict[str, DataObject] = {}
        """
        Mapping of ROM CRC32 hashes to DataObjects.
        CRCs must be all uppercase.
        """

        self.by_md5: dict[str, DataObject] = {}
        self.by_sha1: dict[str, DataObject] = {}
        self.by_serial: dict[str, DataObject] = {}

        self.by_game_id = ChainMap(self.by_crc, self.by_md5, self.by_sha1, self.by_serial)

        for obj in itertools.chain.from_iterable(playlists.values()):
            if obj.ObjectType != 'Game':
                continue

            self.by_id[obj.Id] = obj

            for m in filter(lambda mi: mi.Status == 'Mapped', obj.Metadata):
                self._handle_metadata(obj, m)

            for a in obj.Attributes:
                self._handle_attribute(obj, a)

    def _handle_metadata(self, obj: DataObject, m: MetadataItem) -> None:
        match m.Source:
            case 'IGDB':
                igdb_id = IgdbId(int(m.ImmutableId))
                if igdb_id not in self.by_igdb_id:
                    self.by_igdb_id[igdb_id] = obj
                    self.hasheous_to_igdb[obj.Id] = igdb_id
            case 'RetroAchievements':
                self.supports_achievements.add(obj.Id)

    def _handle_attribute(self, obj: DataObject, a: Attribute) -> None:
        match (a.attributeType, a.attributeName, a.Value):
            case ('EmbeddedList', 'ROMs', roms) if isinstance(roms, Sequence) and not isinstance(roms, str):
                for r in roms:
                    if r.Crc:
                        crc = r.Crc.upper()
                        if crc not in self.by_crc:
                            self.by_crc[crc] = obj

                    if r.Md5:
                        md5 = r.Md5.upper()
                        if md5 not in self.by_md5:
                            self.by_md5[md5] = obj

                    if r.Sha1:
                        sha1 = r.Sha1.upper()
                        if sha1 not in self.by_sha1:
                            self.by_sha1[sha1] = obj

                    if r.Attributes and (serial := r.Attributes.get('serial', None)):
                        serial_upper = serial.upper()
                        if serial_upper not in self.by_serial:
                            self.by_serial[serial_upper] = obj


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

async def load_index(path: Path | str) -> HasheousIndex:
    """
    Load a HasheousIndex from the given pickle file.

    :param path: Path to the pickle file containing the HasheousIndex.

    :return: The loaded HasheousIndex.
    """

    async with aiofiles.open(path, "rb") as index_file:
        index = pickle.load(index_file.raw)

        if not isinstance(index, HasheousIndex):
            raise TypeError(f"Expected HasheousIndex in pickle file at {path}; got {type(index).__name__}")

    return index

DEFAULT_CHUNKSIZE = 16

def create_index(
    zip_paths: Iterable[Path],
    playlists: Iterable[Playlist],
    executor: type[Executor] | Executor | None=None,
    chunksize=DEFAULT_CHUNKSIZE
) -> HasheousIndex:
    """
    Create a HasheousIndex from the given metadata directory for the specified playlists.

    :param metadata_dir: Path to the directory containing Hasheous metadata ZIP files.
    :param playlists: An iterable of Playlist objects to load DataObjects for.

    :return: An index of all loaded DataObjects.
    """

    resolved_zip_paths = {p.resolve() for p in zip_paths if zipfile.is_zipfile(p)}

    def _make_index(zips: dict[str, Sequence[DataObject]]) -> HasheousIndex:
        # A map of dump filenames (minus .zip) to parsed DataObjects.
        # A Hasheous dump can be referenced by multiple IGDB playlists,
        # so we load the ZIP files and merge the results accordingly.

        playlist_map: dict[PlaylistTitle, Iterable[DataObject]] = {}
        for playlist in playlists:
            objects = itertools.chain.from_iterable(zips[d] for d in playlist.hasheous_dirs if d in zips)
            playlist_map[playlist.title] = objects

        return HasheousIndex(playlist_map.items())

    match executor:
        case None:
            zips = dict(map(parse_zip, resolved_zip_paths))
            # A map of dump filenames (minus .zip) to parsed DataObjects.
            # A Hasheous dump can be referenced by multiple IGDB playlists,
            # so we load the ZIP files and merge the results accordingly.

            return _make_index(zips)
        case type() if issubclass(executor, Executor):
            with executor() as e:
                zips = dict(e.map(parse_zip, resolved_zip_paths, chunksize=chunksize))
                return _make_index(zips)
        case Executor():
            with executor as e:
                zips = dict(e.map(parse_zip, resolved_zip_paths, chunksize=chunksize))
                return _make_index(zips)
        case _:
            raise TypeError(f"Expected Executor, executor type, or None; got {type(executor).__name__}")


def read_dump_names(path: str) -> Collection[str]:
    # TODO: Remove this, just read from the PLAYLISTS object
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

ExecutorTypeName: TypeAlias = Literal["none", "process", "thread", "interpreter"]

def executor_type(s: Optional[str]) -> type[Executor] | None:
    match s:
        case None | "none":
            return None
        case "process":
            return ProcessPoolExecutor
        case "thread":
            return ThreadPoolExecutor
        case _:
            raise argparse.ArgumentTypeError(f"Invalid executor type: {s}")

async def handle_index(args: argparse.Namespace) -> None:
    input_paths: Collection[str] = args.paths
    verbose = bool(args.verbose)
    output: Path = args.output
    executor = executor_type(args.executor)
    chunksize: int = args.chunksize

    if verbose:
        print("Input paths:", input_paths)
        print("Output path:", output)
        print("Executor:", executor)

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

    print(f"Indexing all DataObjects...")
    index_start = time.perf_counter_ns()
    index = create_index(zip_paths, PLAYLISTS, executor=executor, chunksize=chunksize)
    index_finish = time.perf_counter_ns()
    print(f"Indexed all DataObjects in {(index_finish - index_start) / 1_000_000:.2f} ms")

    dump_start = time.perf_counter_ns()
    print(f"Saving index to {output}...")
    with open(output, "wb") as out_file:
        pickle.dump(index, out_file, protocol=5)
    dump_finish = time.perf_counter_ns()
    print(f"Saved index to {output} in {(dump_finish - dump_start) / 1_000_000:.2f} ms")

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
        nargs="?",
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
        default="tmp/index/hasheous.pkl",
    )
    index_parser.add_argument(
        "--executor",
        choices=ExecutorTypeName.__args__,
        type=str,
        default="process",
        help="Enable (default) or disable parallel processing"
    )
    index_parser.add_argument(
        "--chunksize",
        type=int,
        default=DEFAULT_CHUNKSIZE,
        help=f"The number of tasks to submit to each worker at a time when using parallel processing (default: {DEFAULT_CHUNKSIZE}). Ignored if not using an executor.",
    )
    index_parser.set_defaults(func=handle_index)

    # Parse arguments and call appropriate handler

    args = parser.parse_args()
    asyncio.run(args.func(args))

__all__ = (
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
    "HasheousId",
    "load_index",
    "HasheousIndex",
    "MediaType",
)

if __name__ == "__main__":
    main()
