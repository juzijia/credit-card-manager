from collections.abc import Iterable
from typing import cast

from app.parsers.abc import AbcStatementParser
from app.parsers.base import (
    BankStatementParser,
    ParsedBillingDocument,
    ParsedSource,
    ParserError,
    billing_document_from_statement,
)
from app.parsers.boc import BocStatementParser
from app.parsers.bocom import BocomStatementParser
from app.parsers.ccb import CcbStatementParser
from app.parsers.cgb import CgbStatementParser
from app.parsers.citic import CiticStatementParser
from app.parsers.cmb import CmbStatementParser
from app.parsers.icbc import IcbcStatementParser
from app.parsers.pab import PingAnStatementParser


class ParserRegistry:
    def __init__(self, parsers: Iterable[BankStatementParser] = ()) -> None:
        self._parsers = tuple(parsers)

    @property
    def supported_banks(self) -> tuple[str, ...]:
        return tuple(parser.bank_code for parser in self._parsers)

    def parser_for(self, source: ParsedSource) -> BankStatementParser:
        matches = [parser for parser in self._parsers if parser.can_handle(source)]
        if not matches:
            raise ParserError("No registered parser recognizes this source")
        if len(matches) > 1:
            raise ParserError("More than one parser recognizes this source")
        return matches[0]

    def parse(self, source: ParsedSource) -> ParsedBillingDocument:
        parser = self.parser_for(source)
        parse_document = getattr(parser, "parse_document", None)
        if callable(parse_document):
            document = parse_document(source)
            if not isinstance(document, ParsedBillingDocument):
                raise TypeError("parse_document() must return ParsedBillingDocument")
            return cast(ParsedBillingDocument, document)
        return billing_document_from_statement(parser.parse(source))


default_registry = ParserRegistry([
    CgbStatementParser(),
    CmbStatementParser(),
    PingAnStatementParser(),
    CiticStatementParser(),
    IcbcStatementParser(),
    CcbStatementParser(),
    BocomStatementParser(),
    AbcStatementParser(),
    BocStatementParser(),
])
