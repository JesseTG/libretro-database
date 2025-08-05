import pyparsing as pp
from dataclasses import dataclass

@dataclass
class ClrMamePro:
    name: str
    description: str
    version: str
    comment: str
    homepage: str

@dataclass
class Rom:
    name: str
    crc: str = None
    md5: str | None = None
    sha1: str | None = None


# Forward declaration for recursive grammar
dat_record = pp.Forward()

# Basic components
key = pp.Word(pp.alphanums + "_-")
quoted_string = pp.QuotedString('"')
unquoted_string = pp.Word(pp.printables, excludeChars='() \t\n\r')

# Value can be quoted string, unquoted string, or nested record
value = quoted_string | unquoted_string | dat_record

# Record content can be either key-value pairs or nested records
record_content = pp.ZeroOrMore(pp.Group(key + value) | dat_record)

# Record structure: record_type ( content )
record_type = pp.Word(pp.alphanums + "_-")
dat_record <<= pp.Group(record_type + pp.Suppress('(') + record_content + pp.Suppress(')'))

# Complete DAT file parser (multiple records)
dat_file = pp.ZeroOrMore(dat_record)

# For convenience, also provide a single record parser
single_record = dat_record
