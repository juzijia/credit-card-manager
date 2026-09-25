import hashlib
from email import policy
from email.message import Message
from email.parser import BytesParser
from pathlib import Path

from app.parsers.base import ParsedSource, ParserError

MAX_EML_BYTES = 10 * 1024 * 1024


def _decode_part(part: Message) -> str:
    payload = part.get_payload(decode=True)
    if payload is None:
        content = part.get_payload()
        return content if isinstance(content, str) else ""

    declared = part.get_content_charset()
    candidates = [declared, "utf-8", "gb18030"]
    for encoding in candidates:
        if not encoding:
            continue
        try:
            return payload.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    raise ParserError("The email HTML encoding could not be decoded")


def _html_part(message: Message) -> Message | None:
    if message.get_content_type() == "text/html":
        return message
    for part in message.walk():
        if (
            part.get_content_type() == "text/html"
            and part.get_content_disposition() != "attachment"
        ):
            return part
    return None


def parse_eml_bytes(
    data: bytes, *, path: Path | None = None, max_bytes: int = MAX_EML_BYTES
) -> ParsedSource:
    if len(data) > max_bytes:
        raise ParserError(f"EML exceeds the {max_bytes}-byte size limit")
    message = BytesParser(policy=policy.default).parsebytes(data)
    html_part = _html_part(message)
    if html_part is None:
        raise ParserError("The email does not contain an HTML body")

    return ParsedSource(
        source_type="eml",
        subject=str(message.get("Subject", "")),
        sender=str(message.get("From", "")),
        html=_decode_part(html_part),
        sha256=hashlib.sha256(data).hexdigest(),
        message_id=str(message.get("Message-ID", "")) or None,
        path=path,
        raw_bytes=data,
    )


def load_eml(path: Path, *, max_bytes: int = MAX_EML_BYTES) -> ParsedSource:
    if path.stat().st_size > max_bytes:
        raise ParserError(f"EML exceeds the {max_bytes}-byte size limit")
    return parse_eml_bytes(path.read_bytes(), path=path, max_bytes=max_bytes)
