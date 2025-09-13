"""
Dictionary definitions taken from https://github.com/gaseous-project/hasheous/blob/main/hasheous-lib/Models/DataObjectItem.cs
"""

from collections.abc import Sequence, Mapping
from typing import TypedDict, Required, Any, Literal, TypeAlias, NotRequired, cast

METADATA_MAP_URL = "https://hasheous.org/api/v1/Dumps/MetadataMap.zip"


class SignatureDataObject(TypedDict, total=False):
    SignatureId: str
    Name: str
    Year: str
    Platform: str
    SourceId: str
    MetadataSource: str


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

class MetadataItem(TypedDict):
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

class MediaType(TypedDict, total=False):
    MediaType: RomTypeName
    Number: int
    Count: int
    Side: str

class RomItem(TypedDict, total=False):
    Score: Required[int]
    Id: str
    Name: str
    Size: int
    Crc: str
    Md5: str
    Sha1: str
    Sha256: str
    Status: str
    Country: Mapping[str, str]
    Language: Mapping[str, str]
    DevelopmentStatus: str
    Attributes: Required[Mapping[str, Any]]
    RomType: Required[RomTypeName]
    RomTypeMedia: str
    MediaDetail: MediaType
    MediaLabel: str
    SignatureSource: SignatureSourceType

AttributeValue: TypeAlias = "DataObject | str | Sequence[RomItem]"

class Attribute(TypedDict):
    Id: NotRequired[int]
    attributeType: AttributeType
    '''Not a typo, the API serializes it this way'''

    attributeName: AttributeName
    '''Not a typo, the API serializes it this way'''

    attributeRelationType: DataObjectType
    '''Not a typo, the API serializes it this way'''

    Value: AttributeValue


class DataObject(TypedDict):
    Id: int
    ObjectType: DataObjectType
    SignatureDataObjects: Sequence[SignatureDataObject]
    Metadata: Sequence[MetadataItem]
    Attributes: Sequence[Attribute]
    CreatedDate: str
    UpdatedDate: str
    Name: str

def get_rom_list(data_object: DataObject) -> Sequence[RomItem]:
    for a in data_object["Attributes"]:
        if a['attributeName'] == 'ROMs' and isinstance(a['Value'], Sequence):
            return cast(Sequence[RomItem], a['Value'])

    raise LookupError(f"No ROMs found in DataObject {data_object['Id']}")

def get_igdb_id(data_object: DataObject) -> int | None:
    for m in data_object["Metadata"]:
        if m['Source'] == 'IGDB' and m['Status'] == 'Mapped':
            try:
                return int(m['ImmutableId'])
            except ValueError:
                return None

    return None

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
]
