"""Property-based test for jar-content validation (task 11.4).

Feature: hellodj-nix-native-delivery, Property 2: Built jars are real and
contain no placeholder marker

Property 2: *For any* jar produced by a Fork_Flake (``Lavalink.jar``,
lavaplayer, ``lavasrc-plugin``, ``youtube-plugin-sabr``) or bundled into the
Lavalink_Image, the jar's manifest declares a ``Main-Class`` or plugin
entrypoint, the jar contains at least one compiled ``.class`` entry, and the jar
contains no ``PLACEHOLDER ARTIFACT`` marker bytes and is not zero-byte.

This exercises the pure :func:`is_real_jar` predicate against *actual jar bytes*
built in memory with :mod:`zipfile` + :class:`io.BytesIO` (using real zip
compression), then re-opened so the descriptor carries each entry's genuine
decompressed data -- exactly what a real gate reads out of a jar. Coverage
spans:

* **real-shaped jars** for the four fork jar outputs -- either a ``Main-Class``
  server jar (Lavalink / lavaplayer) or a plugin-entrypoint jar (lavasrc /
  youtube-source), always carrying >=1 ``.class`` entry and no placeholder
  marker -- which MUST be accepted; and
* **degenerate jars** exhibiting exactly one broken invariant (zero-byte,
  ``PLACEHOLDER ARTIFACT`` bytes, no manifest, no entrypoint, or no ``.class``
  entry) -- which MUST be rejected.

Validates: Requirements 2.6, 4.6
"""

from __future__ import annotations

import io
import zipfile

from hypothesis import given, settings
from hypothesis import strategies as st

from hellodj_platform_logic.jar_validation import (
    MANIFEST_ENTRY,
    PLACEHOLDER_MARKER,
    JarDescriptor,
    is_real_jar,
)

# ---------------------------------------------------------------------------
# Strategy building blocks
# ---------------------------------------------------------------------------

# The four fork jar outputs Property 2 quantifies over. Server-style jars
# (Lavalink, lavaplayer) declare a Main-Class; plugin-style jars (lavasrc,
# youtube-source) declare a Lavalink plugin entrypoint descriptor instead.
_MAIN_CLASS_JARS = ["Lavalink.jar", "lavaplayer"]
_PLUGIN_JARS = ["lavasrc-plugin", "youtube-plugin-sabr"]

# A Java-package-ish identifier segment used to build plausible class names and
# Main-Class / plugin-class values. Kept to a small charset so datasets are
# cheap; the predicate reasons over structure, not the identifier text.
_IDENT = st.text(
    alphabet=st.characters(min_codepoint=ord("a"), max_codepoint=ord("z")),
    min_size=1,
    max_size=6,
)

# A .class file body starts with the JVM magic bytes. The exact bytes are
# irrelevant to the predicate (it keys off the .class name), but real class
# bytes keep the synthetic jar faithful.
_CLASS_MAGIC = b"\xca\xfe\xba\xbe"


@st.composite
def _class_names(draw: st.DrawFn) -> list[str]:
    """Generate a non-empty list of distinct ``.class`` entry names."""
    count = draw(st.integers(min_value=1, max_value=4))
    names: list[str] = []
    for _ in range(count):
        segments = draw(st.lists(_IDENT, min_size=1, max_size=3))
        cls = draw(_IDENT).capitalize()
        names.append("/".join([*segments, cls]) + ".class")
    # De-duplicate while preserving order; zip entries must be unique.
    seen: set[str] = set()
    return [n for n in names if not (n in seen or seen.add(n))]


def _dotted(draw: st.DrawFn) -> str:
    """Draw a dotted class name like ``com.example.Main``."""
    segments = draw(st.lists(_IDENT, min_size=1, max_size=3))
    return ".".join([*segments, draw(_IDENT).capitalize()])


def _build_descriptor(entries: dict[str, bytes]) -> JarDescriptor:
    """Build real (compressed) jar bytes, then read them back into a descriptor.

    The jar is written with real zip compression and re-opened, so the
    descriptor's ``entries`` map holds each entry's genuine *decompressed*
    bytes and ``size_bytes`` is the actual on-disk jar size -- proving the
    predicate works against real jar bytes, not a hand-built shortcut.

    Args:
        entries: Mapping of zip entry name to its raw (uncompressed) bytes.

    Returns:
        A :class:`JarDescriptor` built from the real jar's contents.
    """
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as jar:
        for name, data in entries.items():
            jar.writestr(name, data)
    content = buffer.getvalue()

    with zipfile.ZipFile(io.BytesIO(content), "r") as jar:
        read_entries = {name: jar.read(name) for name in jar.namelist()}

    return JarDescriptor(entries=read_entries, size_bytes=len(content))


@st.composite
def real_jars(draw: st.DrawFn) -> JarDescriptor:
    """Generate a real-shaped jar for one of the four fork jar outputs.

    Every generated jar has a manifest declaring an entrypoint (a ``Main-Class``
    for server jars, or a plugin descriptor for plugin jars), at least one
    compiled ``.class`` entry, non-zero size, and no placeholder marker -- so it
    MUST be accepted by :func:`is_real_jar`.
    """
    jar_kind = draw(st.sampled_from(_MAIN_CLASS_JARS + _PLUGIN_JARS))
    entries: dict[str, bytes] = {
        name: _CLASS_MAGIC + draw(st.binary(min_size=0, max_size=8))
        for name in draw(_class_names())
    }

    if jar_kind in _MAIN_CLASS_JARS:
        main_class = _dotted(draw)
        entries[MANIFEST_ENTRY] = (
            f"Manifest-Version: 1.0\nMain-Class: {main_class}\n".encode()
        )
    else:
        # Plugin jars declare their entrypoint via a plugin descriptor, not a
        # Main-Class. Include a manifest without Main-Class plus the descriptor.
        entries[MANIFEST_ENTRY] = b"Manifest-Version: 1.0\n"
        descriptor_name = f"lavalink.plugins/{draw(_IDENT)}.properties"
        key = draw(st.sampled_from(["plugin.class", "pluginClass"]))
        entries[descriptor_name] = f"{key}={_dotted(draw)}\n".encode()

    return _build_descriptor(entries)


# Degenerate jar "kinds" -- each breaks exactly one invariant.
_ZERO_BYTE = "zero_byte"
_PLACEHOLDER = "placeholder"
_NO_MANIFEST = "no_manifest"
_NO_ENTRYPOINT = "no_entrypoint"
_NO_CLASS = "no_class"


@st.composite
def degenerate_jars(draw: st.DrawFn) -> JarDescriptor:
    """Generate a degenerate jar that breaks exactly one Property-2 invariant.

    Each generated jar MUST be rejected by :func:`is_real_jar`.
    """
    kind = draw(
        st.sampled_from(
            [_ZERO_BYTE, _PLACEHOLDER, _NO_MANIFEST, _NO_ENTRYPOINT, _NO_CLASS]
        )
    )
    class_entries = {
        name: _CLASS_MAGIC for name in draw(_class_names())
    }
    main_manifest = (
        f"Manifest-Version: 1.0\nMain-Class: {_dotted(draw)}\n".encode()
    )

    if kind == _ZERO_BYTE:
        # A zero-byte artifact -- no jar structure at all.
        return JarDescriptor(entries={}, size_bytes=0)

    if kind == _PLACEHOLDER:
        # A well-formed jar shape but carrying the placeholder marker bytes in
        # an entry, exactly as mkPlaceholderJar emits.
        entries = dict(class_entries)
        entries[MANIFEST_ENTRY] = main_manifest
        entries["META-INF/placeholder.txt"] = (
            b"PLACEHOLDER ARTIFACT: not a real build output"
        )
        descriptor = _build_descriptor(entries)
        # The marker survives compression + read-back into the entry data.
        assert any(
            PLACEHOLDER_MARKER in data for data in descriptor.entries.values()
        )
        return descriptor

    if kind == _NO_MANIFEST:
        # Compiled classes but no manifest -> no declared entrypoint source.
        return _build_descriptor(dict(class_entries))

    if kind == _NO_ENTRYPOINT:
        # Manifest present with classes, but neither a Main-Class nor a plugin
        # entrypoint descriptor.
        entries = dict(class_entries)
        entries[MANIFEST_ENTRY] = b"Manifest-Version: 1.0\n"
        return _build_descriptor(entries)

    # _NO_CLASS: manifest with a Main-Class but zero compiled .class entries.
    return _build_descriptor(
        {
            MANIFEST_ENTRY: main_manifest,
            "META-INF/notes.txt": b"resources only, no classes",
        }
    )


# ---------------------------------------------------------------------------
# Properties
# ---------------------------------------------------------------------------


@settings(max_examples=200)
@given(descriptor=real_jars())
def test_real_fork_jars_are_accepted(descriptor: JarDescriptor) -> None:
    """Feature: hellodj-nix-native-delivery, Property 2.

    A real-shaped jar for any of the four fork jar outputs -- manifest declaring
    a Main-Class or plugin entrypoint, >=1 ``.class`` entry, no placeholder
    marker, non-zero -- is accepted.

    Validates: Requirements 2.6, 4.6
    """
    # Sanity: the generated jar genuinely satisfies each structural invariant,
    # exercised against the real (read-back) jar entries.
    assert descriptor.size_bytes > 0, "real jar must be non-zero-byte"
    assert all(
        PLACEHOLDER_MARKER not in data for data in descriptor.entries.values()
    )
    assert MANIFEST_ENTRY in descriptor.entries
    assert any(name.endswith(".class") for name in descriptor.entries)

    assert is_real_jar(descriptor) is True


@settings(max_examples=200)
@given(descriptor=degenerate_jars())
def test_placeholder_and_degenerate_jars_are_rejected(
    descriptor: JarDescriptor,
) -> None:
    """Feature: hellodj-nix-native-delivery, Property 2.

    A jar that breaks exactly one invariant -- zero-byte, ``PLACEHOLDER
    ARTIFACT`` bytes, missing manifest, missing entrypoint, or no ``.class``
    entry -- is rejected.

    Validates: Requirements 2.6, 4.6
    """
    assert is_real_jar(descriptor) is False
