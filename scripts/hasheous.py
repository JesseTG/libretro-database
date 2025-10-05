"""
Dictionary definitions taken from https://github.com/gaseous-project/hasheous/blob/main/hasheous-lib/Models/DataObjectItem.cs
"""

import asyncio
import dataclasses
import itertools
from pathlib import Path
import zipfile

from collections.abc import Sequence, Mapping
from typing import Literal, Optional, TypeAlias, Union
from zipfile import ZipFile

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
    Attributes: Mapping[str, str] | None = None
    RomType: RomTypeName
    Id: str | None = None
    Name: str | None = None
    Size: int | None = None
    Crc: str | None = None
    Md5: str | None = None
    Sha1: str | None = None
    Sha256: str | None = None
    Status: str | None = None
    Country: Mapping[str, str] | None = None
    Language: Mapping[str, str] | None = None
    DevelopmentStatus: str | None = None
    RomTypeMedia: str | None = None
    MediaDetail: MediaType | None = None
    MediaLabel: str | None = None
    SignatureSource: SignatureSourceType | None = None

AttributeValue: TypeAlias = Union["DataObject", list[RomItem], str]

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

    def has_rom(self, crc: str | None, md5: str | None, sha1: str | None) -> bool:
        for rom in self.rom_list:
            if crc and rom.Crc and (rom.Crc.lower() == crc.lower()):
                return True
            if md5 and rom.Md5 and (rom.Md5.lower() == md5.lower()):
                return True
            if sha1 and rom.Sha1 and (rom.Sha1.lower() == sha1.lower()):
                return True

        return False

    @property
    def rom_list(self) -> Sequence[RomItem]:
        """Return the list of ROMs, or an empty tuple if none are present."""
        for a in self.Attributes:
            if a.attributeName == 'ROMs' and isinstance(a.Value, Sequence) and not isinstance(a.Value, str):
                return a.Value

        return ()

    @property
    def igdb_id(self) -> int | None:
        for m in self.Metadata:
            if m.Source == 'IGDB' and m.Status == 'Mapped':
                try:
                    return int(m.ImmutableId)
                except ValueError:
                    return None

        return None

DataObjectCodec: typelib.Codec[DataObject] = typelib.codec(DataObject)

async def load_dataobjects(metadata_zip_path: Path, hasheous_dirs: Mapping[str, Sequence[str]]) -> Mapping[str, Sequence[DataObject]]:
    """
    Load Hasheous DataObjects from the given metadata ZIP file for the specified playlists.

    :param metadata_zip_path: Path to the Hasheous MetadataMap.zip file.
    :param hasheous_dirs: A mapping of playlist names to the directories
    in MetadataMap.zip to load DataObjects from.

    :return: A mapping of playlist names to the DataObjects representing
    the games in those playlists.
    """
    # TODO: If metadata_map is not given, download it from https://hasheous.org/api/v1/Dumps/MetadataMap.zip

    result: dict[str, Sequence[DataObject]] = {}
    with ZipFile(metadata_zip_path) as metadata_zip:
        root = zipfile.Path(metadata_zip)
        def get_zip_path(path: str) -> zipfile.Path:
            return root.joinpath(*path.split('/'))

        def parse_dataobject(path: zipfile.Path) -> DataObject:
            data = path.read_bytes()
            return DataObjectCodec.decode(data)

        for playlist_name, dir_paths in hasheous_dirs.items():
            # For each relevant directory in the zip file...

            zip_paths = map(get_zip_path, dir_paths)
            # Get the path object for each directory

            paths = itertools.chain.from_iterable(p.iterdir() for p in zip_paths)
            # Iterate over each directory's contents

            json_paths = filter(lambda p: p.is_file() and p.suffix == '.json', paths)
            # Ignore everything that isn't a JSON file

            objects = map(parse_dataobject, json_paths)
            # Parse each JSON file into a DataObject

            result[playlist_name] = tuple(objects)
            # Then add them to the result list

            await asyncio.sleep(0)
            # Let the event loop have a turn; since this method is likely CPU bound
            # and Python's GIL prevents true thread parallelism,
            # we need to do this to avoid blocking other tasks.

    return result

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
