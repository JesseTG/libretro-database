"""
Dictionary definitions taken from https://github.com/gaseous-project/hasheous/blob/main/hasheous-lib/Models/DataObjectItem.cs
"""

import asyncio
import dataclasses
import zipfile

from collections.abc import Collection, Sequence, Mapping
from typing import Iterable, Literal, TypeAlias
from zipfile import ZipFile

import typelib

METADATA_MAP_URL = "https://hasheous.org/api/v1/Dumps/MetadataMap.zip"


@dataclasses.dataclass(frozen=True, kw_only=True, slots=True)
class SignatureDataObject:
    SignatureId: str | None = None
    Name: str | None = None
    Year: str | None = None
    Platform: str | None = None
    SourceId: str | None = None
    Publisher: str | None = None
    MetadataSource: str | None = None


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
    "TheGamesDB",
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

DataObjectType: TypeAlias = Literal["Company", "Platform", "Game", "ROM", "App"]
RomTypeName: TypeAlias = Literal["Unknown", "Disc", "Disk", "File", "Part", "Tape", "Side"]
SignatureSourceType: TypeAlias = Literal[
    "None",
    "TOSEC",
    "MAMEArcade",
    "MAMEMess",
    "NoIntro",
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
    MediaType: RomTypeName | None = None
    Media: str | None = None
    Number: int | None = None
    Count: int | None = None
    Side: str | None = None

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

AttributeValue: TypeAlias = "DataObject | str | Sequence[RomItem]"

@dataclasses.dataclass(frozen=True, kw_only=True, slots=True)
class Attribute:
    attributeType: AttributeType
    '''Not a typo, the API serializes it this way'''

    attributeName: AttributeName
    '''Not a typo, the API serializes it this way'''

    attributeRelationType: DataObjectType
    '''Not a typo, the API serializes it this way'''

    Value: AttributeValue
    Id: int | None = None


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

async def load_dataobjects(metadata_zip_path: str, hasheous_dirs: Iterable[str]) -> Collection[DataObject]:
    """Load Hasheous DataObjects from the given metadata ZIP file for the specified playlists."""
    # TODO: If metadata_map is not given, download it from https://hasheous.org/api/v1/Dumps/MetadataMap.zip

    result: list[DataObject] = []
    with ZipFile(metadata_zip_path) as metadata_zip:
        root = zipfile.Path(metadata_zip)
        def get_zip_path(p: str) -> zipfile.Path:
            return root.joinpath(*p.split('/'))

        def parse_dataobject(path: zipfile.Path) -> DataObject:
            data = path.read_bytes()
            return DataObjectCodec.decode(data)

        for d in map(get_zip_path, hasheous_dirs):
            # For each relevant directory in the zip file...

            json_paths = filter(lambda p: p.is_file() and p.suffix == '.json', d.iterdir())
            # Get the path to every JSON file in that directory

            objects = map(parse_dataobject, json_paths)
            # Parse each JSON file into a DataObject
            
            result.extend(objects)
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
