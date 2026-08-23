"""Leitura estrita da identidade compilada de um APK sem depender do Android SDK."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import struct
import zipfile
from pathlib import Path
from typing import Any
from xml.etree import ElementTree


ANDROID_NS = "http://schemas.android.com/apk/res/android"
NO_INDEX = 0xFFFFFFFF
RES_STRING_POOL_TYPE = 0x0001
RES_XML_TYPE = 0x0003
RES_XML_RESOURCE_MAP_TYPE = 0x0180
RES_XML_START_ELEMENT_TYPE = 0x0102
UTF8_FLAG = 0x00000100
TYPE_STRING = 0x03
TYPE_FIRST_INT = 0x10
TYPE_LAST_INT = 0x1F
ANDROID_VERSION_CODE_ID = 0x0101021B
ANDROID_VERSION_NAME_ID = 0x0101021C
MAX_MANIFEST_BYTES = 16 * 1024 * 1024
TOOLCHAIN_ASSET_DIR = Path("app/src/main/assets/core-linux/android-builder")
TOOLCHAIN_CHUNKS_MANIFEST = "android-builder-toolchain.parts.json"
TOOLCHAIN_CHUNK_PATTERN = re.compile(r"android-builder-toolchain\.part-(\d{3})\.cwpart")


class ApkIdentityError(ValueError):
    """O APK não contém uma identidade Android compilada válida e verificável."""


def _require(data: bytes, offset: int, size: int, label: str) -> None:
    if offset < 0 or size < 0 or offset + size > len(data):
        raise ApkIdentityError(f"AndroidManifest.xml truncado em {label}")


def _u16(data: bytes, offset: int, label: str = "u16") -> int:
    _require(data, offset, 2, label)
    return struct.unpack_from("<H", data, offset)[0]


def _u32(data: bytes, offset: int, label: str = "u32") -> int:
    _require(data, offset, 4, label)
    return struct.unpack_from("<I", data, offset)[0]


def _decode_length8(data: bytes, offset: int) -> tuple[int, int]:
    _require(data, offset, 1, "comprimento UTF-8")
    first = data[offset]
    if first & 0x80:
        _require(data, offset, 2, "comprimento UTF-8 longo")
        return ((first & 0x7F) << 8) | data[offset + 1], offset + 2
    return first, offset + 1


def _decode_length16(data: bytes, offset: int) -> tuple[int, int]:
    first = _u16(data, offset, "comprimento UTF-16")
    if first & 0x8000:
        second = _u16(data, offset + 2, "comprimento UTF-16 longo")
        return ((first & 0x7FFF) << 16) | second, offset + 4
    return first, offset + 2


def _read_string_pool(data: bytes, chunk_offset: int, header_size: int, chunk_size: int) -> list[str]:
    if header_size < 28:
        raise ApkIdentityError("string pool com header inválido")
    string_count = _u32(data, chunk_offset + 8, "stringCount")
    style_count = _u32(data, chunk_offset + 12, "styleCount")
    flags = _u32(data, chunk_offset + 16, "string flags")
    strings_start = _u32(data, chunk_offset + 20, "stringsStart")
    styles_start = _u32(data, chunk_offset + 24, "stylesStart")
    if string_count > 1_000_000 or style_count > 1_000_000:
        raise ApkIdentityError("string pool excessivo")
    offsets_start = chunk_offset + header_size
    offsets_bytes = (string_count + style_count) * 4
    _require(data, offsets_start, offsets_bytes, "offsets da string pool")
    string_data_start = chunk_offset + strings_start
    chunk_end = chunk_offset + chunk_size
    string_data_end = chunk_offset + styles_start if styles_start else chunk_end
    if string_data_start < offsets_start or string_data_start > string_data_end or string_data_end > chunk_end:
        raise ApkIdentityError("faixa inválida da string pool")

    utf8 = bool(flags & UTF8_FLAG)
    strings: list[str] = []
    for index in range(string_count):
        relative = _u32(data, offsets_start + index * 4, "offset de string")
        cursor = string_data_start + relative
        if cursor < string_data_start or cursor >= string_data_end:
            raise ApkIdentityError("offset de string fora da string pool")
        if utf8:
            _utf16_length, cursor = _decode_length8(data, cursor)
            byte_length, cursor = _decode_length8(data, cursor)
            _require(data, cursor, byte_length + 1, "string UTF-8")
            raw = data[cursor:cursor + byte_length]
            if data[cursor + byte_length] != 0:
                raise ApkIdentityError("string UTF-8 sem terminador")
            strings.append(raw.decode("utf-8", errors="strict"))
        else:
            char_length, cursor = _decode_length16(data, cursor)
            byte_length = char_length * 2
            _require(data, cursor, byte_length + 2, "string UTF-16")
            raw = data[cursor:cursor + byte_length]
            if data[cursor + byte_length:cursor + byte_length + 2] != b"\0\0":
                raise ApkIdentityError("string UTF-16 sem terminador")
            strings.append(raw.decode("utf-16le", errors="strict"))
    return strings


def _pool_string(strings: list[str], index: int) -> str:
    if index == NO_INDEX:
        return ""
    if index < 0 or index >= len(strings):
        raise ApkIdentityError("índice de string inválido no manifest")
    return strings[index]


def _typed_value(strings: list[str], raw_index: int, data_type: int, value_data: int) -> Any:
    if raw_index != NO_INDEX:
        return _pool_string(strings, raw_index)
    if data_type == TYPE_STRING:
        return _pool_string(strings, value_data)
    if TYPE_FIRST_INT <= data_type <= TYPE_LAST_INT:
        return int(value_data)
    return None


def _parse_text_manifest(raw: bytes) -> dict[str, Any]:
    try:
        root = ElementTree.fromstring(raw.decode("utf-8-sig"))
    except Exception as exc:
        raise ApkIdentityError(f"AndroidManifest.xml textual inválido: {type(exc).__name__}") from exc
    if root.tag.rsplit("}", 1)[-1] != "manifest":
        raise ApkIdentityError("raiz do AndroidManifest.xml não é manifest")
    package_name = str(root.attrib.get("package") or "").strip()
    version_name = str(root.attrib.get(f"{{{ANDROID_NS}}}versionName") or "").strip()
    raw_code = root.attrib.get(f"{{{ANDROID_NS}}}versionCode")
    try:
        version_code = int(str(raw_code or "0"), 0)
    except Exception as exc:
        raise ApkIdentityError("versionCode textual inválido") from exc
    return _finish_identity(package_name, version_name, version_code)


def _finish_identity(package_name: str, version_name: str, version_code: int) -> dict[str, Any]:
    package_name = str(package_name or "").strip()
    version_name = str(version_name or "").strip()
    try:
        version_code = int(version_code)
    except Exception as exc:
        raise ApkIdentityError("versionCode compilado inválido") from exc
    if not package_name:
        raise ApkIdentityError("package ausente no AndroidManifest.xml compilado")
    if not version_name:
        raise ApkIdentityError("versionName ausente ou não resolvível no APK")
    if version_code <= 0:
        raise ApkIdentityError("versionCode ausente ou inválido no APK")
    return {
        "packageName": package_name,
        "versionName": version_name,
        "versionCode": version_code,
    }


def parse_android_manifest_identity(raw: bytes) -> dict[str, Any]:
    """Extrai package/versionName/versionCode do manifest textual ou AXML binário."""
    if not isinstance(raw, (bytes, bytearray)) or not raw:
        raise ApkIdentityError("AndroidManifest.xml vazio")
    data = bytes(raw)
    if len(data) > MAX_MANIFEST_BYTES:
        raise ApkIdentityError("AndroidManifest.xml excede o limite")
    if data.lstrip().startswith(b"<"):
        return _parse_text_manifest(data)

    if len(data) < 8 or _u16(data, 0, "tipo XML") != RES_XML_TYPE:
        raise ApkIdentityError("AndroidManifest.xml não é AXML binário")
    root_header_size = _u16(data, 2, "header XML")
    root_size = _u32(data, 4, "tamanho XML")
    if root_header_size < 8 or root_size < root_header_size or root_size > len(data):
        raise ApkIdentityError("header AXML inválido")

    strings: list[str] = []
    resource_map: list[int] = []
    offset = root_header_size
    while offset < root_size:
        _require(data, offset, 8, "chunk AXML")
        chunk_type = _u16(data, offset, "tipo de chunk")
        header_size = _u16(data, offset + 2, "header de chunk")
        chunk_size = _u32(data, offset + 4, "tamanho de chunk")
        if header_size < 8 or chunk_size < header_size or offset + chunk_size > root_size:
            raise ApkIdentityError("chunk AXML inválido")
        if chunk_type == RES_STRING_POOL_TYPE:
            strings = _read_string_pool(data, offset, header_size, chunk_size)
        elif chunk_type == RES_XML_RESOURCE_MAP_TYPE:
            count = (chunk_size - header_size) // 4
            resource_map = [_u32(data, offset + header_size + index * 4, "resource map") for index in range(count)]
        elif chunk_type == RES_XML_START_ELEMENT_TYPE:
            if not strings:
                raise ApkIdentityError("elemento AXML antes da string pool")
            if header_size < 16 or chunk_size < 36:
                raise ApkIdentityError("start element AXML inválido")
            ext = offset + 16
            element_name_index = _u32(data, ext + 4, "nome do elemento")
            if _pool_string(strings, element_name_index) != "manifest":
                offset += chunk_size
                continue
            attribute_start = _u16(data, ext + 8, "attributeStart")
            attribute_size = _u16(data, ext + 10, "attributeSize")
            attribute_count = _u16(data, ext + 12, "attributeCount")
            if attribute_size < 20 or attribute_count > 4096:
                raise ApkIdentityError("atributos AXML inválidos")
            attrs_offset = ext + attribute_start
            attrs_end = attrs_offset + attribute_count * attribute_size
            if attrs_offset < ext or attrs_end > offset + chunk_size:
                raise ApkIdentityError("faixa de atributos AXML inválida")

            package_name = ""
            version_name = ""
            version_code = 0
            for index in range(attribute_count):
                attr = attrs_offset + index * attribute_size
                namespace_index = _u32(data, attr, "namespace do atributo")
                name_index = _u32(data, attr + 4, "nome do atributo")
                raw_index = _u32(data, attr + 8, "valor bruto do atributo")
                typed_size = _u16(data, attr + 12, "typed value size")
                data_type = data[attr + 15]
                value_data = _u32(data, attr + 16, "typed value data")
                if typed_size < 8:
                    raise ApkIdentityError("typed value inválido no AXML")
                name = _pool_string(strings, name_index)
                namespace = _pool_string(strings, namespace_index)
                resource_id = resource_map[name_index] if 0 <= name_index < len(resource_map) else 0
                value = _typed_value(strings, raw_index, data_type, value_data)
                if name == "package" and not namespace:
                    package_name = str(value or "").strip()
                elif resource_id == ANDROID_VERSION_CODE_ID or (name == "versionCode" and namespace == ANDROID_NS):
                    if isinstance(value, int):
                        version_code = value
                    elif value is not None:
                        try:
                            version_code = int(str(value), 0)
                        except Exception as exc:
                            raise ApkIdentityError("versionCode compilado não é inteiro") from exc
                elif resource_id == ANDROID_VERSION_NAME_ID or (name == "versionName" and namespace == ANDROID_NS):
                    if value is not None:
                        version_name = str(value).strip()
            return _finish_identity(package_name, version_name, version_code)
        offset += chunk_size
    raise ApkIdentityError("elemento manifest não encontrado no AXML")


def inspect_apk_identity(apk_path: str | Path) -> dict[str, Any]:
    """Lê a identidade real do APK e valida a integridade mínima do arquivo ZIP."""
    path = Path(apk_path)
    if not path.is_file():
        raise ApkIdentityError("APK não encontrado")
    try:
        with zipfile.ZipFile(path) as archive:
            bad = archive.testzip()
            if bad:
                raise ApkIdentityError(f"APK corrompido em {bad}")
            names = set(archive.namelist())
            if "AndroidManifest.xml" not in names or "classes.dex" not in names:
                raise ApkIdentityError("arquivo não contém manifest/classes de APK")
            info = archive.getinfo("AndroidManifest.xml")
            if int(info.file_size or 0) <= 0 or int(info.file_size or 0) > MAX_MANIFEST_BYTES:
                raise ApkIdentityError("AndroidManifest.xml com tamanho inválido")
            raw = archive.read("AndroidManifest.xml")
    except ApkIdentityError:
        raise
    except Exception as exc:
        raise ApkIdentityError(f"APK inválido: {type(exc).__name__}: {exc}") from exc
    return parse_android_manifest_identity(raw)


def assert_expected_apk_identity(
    identity: dict[str, Any],
    *,
    expected_package: str = "dev.core.worker",
    expected_version_name: str = "",
    expected_version_code: int = 0,
) -> dict[str, Any]:
    """Reprova metadados externos que tentem renomear um APK diferente."""
    package_name = str(identity.get("packageName") or "").strip()
    version_name = str(identity.get("versionName") or "").strip()
    version_code = int(identity.get("versionCode") or 0)
    if expected_package and package_name != expected_package:
        raise ApkIdentityError(f"package do APK divergente: {package_name or '?'} != {expected_package}")
    if expected_version_name and version_name != str(expected_version_name).strip():
        raise ApkIdentityError(f"versionName do APK divergente: binário={version_name or '?'} solicitado={expected_version_name}")
    if int(expected_version_code or 0) > 0 and version_code != int(expected_version_code):
        raise ApkIdentityError(f"versionCode do APK divergente: binário={version_code} solicitado={int(expected_version_code)}")
    return identity


def validate_toolchain_chunk_assets(project_dir: str | Path) -> dict[str, Any]:
    """Valida o envelope particionado sem reconstruir o ZIP na memória."""
    asset_dir = Path(project_dir) / TOOLCHAIN_ASSET_DIR
    descriptor_path = asset_dir / TOOLCHAIN_CHUNKS_MANIFEST
    result: dict[str, Any] = {"ok": False, "manifest_path": str(descriptor_path)}
    try:
        if not descriptor_path.is_file() or descriptor_path.stat().st_size > 1024 * 1024:
            raise ValueError("manifesto de partes ausente ou grande demais")
        descriptor = json.loads(descriptor_path.read_text(encoding="utf-8"))
        if not isinstance(descriptor, dict) or descriptor.get("schema") != "core-worker-toolchain-chunks-v1":
            raise ValueError("schema do envelope particionado inválido")
        if int(descriptor.get("version") or 0) != 1:
            raise ValueError("versão do envelope particionado inválida")
        archive = descriptor.get("archive") if isinstance(descriptor.get("archive"), dict) else {}
        expected_bytes = int(archive.get("bytes") or 0)
        expected_sha = str(archive.get("sha256") or "").lower()
        if expected_bytes < 1024 * 1024 or expected_bytes > 1024 * 1024 * 1024:
            raise ValueError("tamanho total do toolchain inválido")
        if not re.fullmatch(r"[0-9a-f]{64}", expected_sha):
            raise ValueError("sha256 total do toolchain inválido")
        chunk_size = int(descriptor.get("chunkSize") or 0)
        if chunk_size < 4 * 1024 * 1024 or chunk_size > 32 * 1024 * 1024:
            raise ValueError("tamanho das partes fora do limite")
        parts = descriptor.get("parts") if isinstance(descriptor.get("parts"), list) else []
        if not 1 <= len(parts) <= 256:
            raise ValueError("quantidade de partes inválida")
        declared_total = sum(int(part.get("bytes") or 0) for part in parts if isinstance(part, dict))
        if declared_total != expected_bytes:
            raise ValueError("soma declarada das partes diverge do tamanho total")
        full_digest = hashlib.sha256()
        total = 0
        declared: set[str] = set()
        for index, part in enumerate(parts):
            if not isinstance(part, dict):
                raise ValueError("entrada de parte inválida")
            name = str(part.get("name") or "")
            match = TOOLCHAIN_CHUNK_PATTERN.fullmatch(name)
            if match is None or int(match.group(1)) != index or name in declared:
                raise ValueError(f"nome/ordem de parte inválido: {name or '?'}")
            declared.add(name)
            expected_part_bytes = int(part.get("bytes") or 0)
            expected_part_sha = str(part.get("sha256") or "").lower()
            if expected_part_bytes <= 0 or expected_part_bytes > chunk_size:
                raise ValueError(f"tamanho inválido da parte {name}")
            if not re.fullmatch(r"[0-9a-f]{64}", expected_part_sha):
                raise ValueError(f"sha256 inválido da parte {name}")
            path = asset_dir / name
            if not path.is_file() or path.stat().st_size != expected_part_bytes:
                raise ValueError(f"parte ausente/truncada: {name}")
            part_digest = hashlib.sha256()
            with path.open("rb") as handle:
                for block in iter(lambda: handle.read(1024 * 1024), b""):
                    part_digest.update(block)
                    full_digest.update(block)
                    total += len(block)
                    if total > expected_bytes:
                        raise ValueError("partes excedem o tamanho total declarado")
            if part_digest.hexdigest() != expected_part_sha:
                raise ValueError(f"sha256 divergente da parte {name}")
        actual = {path.name for path in asset_dir.glob("android-builder-toolchain.part-*.cwpart") if path.is_file()}
        if actual != declared:
            raise ValueError("conjunto de partes contém arquivos extras ou ausentes")
        if total != expected_bytes or full_digest.hexdigest() != expected_sha:
            raise ValueError("tamanho/sha256 total do toolchain divergente")
        return {
            "ok": True,
            "manifest_path": str(descriptor_path),
            "bytes": total,
            "sha256": expected_sha,
            "parts": len(parts),
            "chunk_size": chunk_size,
            "toolchain": descriptor.get("toolchain") if isinstance(descriptor.get("toolchain"), dict) else {},
        }
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
        return result


def publish_toolchain_chunk_assets(
    archive_path: str | Path,
    project_dir: str | Path,
    *,
    chunk_size: int = 16 * 1024 * 1024,
) -> dict[str, Any]:
    """Divide o ZIP grande em assets pequenos, publicando o manifesto por último."""
    archive_path = Path(archive_path)
    project_dir = Path(project_dir)
    chunk_size = max(4 * 1024 * 1024, min(32 * 1024 * 1024, int(chunk_size)))
    if not archive_path.is_file() or not 1024 * 1024 <= archive_path.stat().st_size <= 1024 * 1024 * 1024:
        raise ValueError("ZIP do toolchain ausente ou fora do limite")
    asset_dir = project_dir / TOOLCHAIN_ASSET_DIR
    staging = asset_dir.parent / f".{asset_dir.name}-chunks-{os.getpid()}"
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True, exist_ok=False)
    parts: list[dict[str, Any]] = []
    full_digest = hashlib.sha256()
    total = 0
    try:
        with archive_path.open("rb") as source:
            index = 0
            while True:
                first = source.read(min(1024 * 1024, chunk_size))
                if not first:
                    break
                name = f"android-builder-toolchain.part-{index:03d}.cwpart"
                target = staging / name
                remaining = chunk_size
                part_digest = hashlib.sha256()
                part_bytes = 0
                with target.open("wb") as output:
                    block = first
                    while block:
                        output.write(block)
                        part_digest.update(block)
                        full_digest.update(block)
                        part_bytes += len(block)
                        total += len(block)
                        remaining -= len(block)
                        if remaining <= 0:
                            break
                        block = source.read(min(1024 * 1024, remaining))
                parts.append({"name": name, "bytes": part_bytes, "sha256": part_digest.hexdigest()})
                index += 1
                if index > 256:
                    raise ValueError("toolchain exige partes demais")
        with zipfile.ZipFile(archive_path) as archive:
            manifest_raw = archive.read("manifest.json")
        manifest = json.loads(manifest_raw.decode("utf-8"))
        descriptor = {
            "schema": "core-worker-toolchain-chunks-v1",
            "version": 1,
            "chunkSize": chunk_size,
            "archive": {
                "filename": "android-builder-toolchain.zip",
                "bytes": total,
                "sha256": full_digest.hexdigest(),
            },
            "toolchain": {
                "schema": manifest.get("schema"),
                "version": manifest.get("version"),
                "runtimeLibrariesStrategy": (manifest.get("runtimeLibraries") or {}).get("strategy"),
                "gradleLauncherStrategy": (manifest.get("gradleLauncher") or {}).get("strategy"),
                "validationStrategy": (manifest.get("validation") or {}).get("strategy"),
            },
            "parts": parts,
        }
        (staging / TOOLCHAIN_CHUNKS_MANIFEST).write_text(
            json.dumps(descriptor, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )
        asset_dir.mkdir(parents=True, exist_ok=True)
        for old in asset_dir.glob("android-builder-toolchain.part-*.cwpart"):
            old.unlink()
        for part in parts:
            (staging / part["name"]).replace(asset_dir / part["name"])
        (staging / TOOLCHAIN_CHUNKS_MANIFEST).replace(asset_dir / TOOLCHAIN_CHUNKS_MANIFEST)
        legacy = asset_dir / "android-builder-toolchain.zip"
        if legacy.exists():
            legacy.unlink()
        validated = validate_toolchain_chunk_assets(project_dir)
        if not validated.get("ok"):
            raise ValueError("envelope particionado inválido: " + str(validated.get("error") or "erro desconhecido"))
        return {**validated, "generated": True, "transport": "chunked-assets-v1"}
    finally:
        shutil.rmtree(staging, ignore_errors=True)
