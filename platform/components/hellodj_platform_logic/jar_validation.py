"""Pure jar-content validation for the fork jar outputs (Property 2).

This module holds the pure decision function that classifies a built jar's
*structure* as a "real" runnable jar versus a placeholder/degenerate artifact,
using exactly the invariants the fork flake ``checks`` encode:

* the jar is not zero-byte;
* the jar's manifest (``META-INF/MANIFEST.MF``) declares a ``Main-Class`` **or**
  a Lavalink plugin entrypoint descriptor (a plugin ``.properties`` entry under
  ``lavalink.plugins/`` carrying a ``plugin.class``/``pluginClass`` key naming
  the plugin class, mirroring how Lavalink plugin jars declare their
  entrypoint);
* the jar contains at least one compiled ``.class`` entry;
* no jar entry contains the ``PLACEHOLDER ARTIFACT`` marker bytes (the text
  emitted by the ``mkPlaceholderJar`` derivations this migration removes).

The predicate reasons over a *jar descriptor* -- the **decompressed** contents
of each jar entry (name -> bytes) plus the total on-disk byte size -- because a
jar is a zip whose entries are typically compressed, so the entrypoint /
placeholder markers live in the *entry data*, not in the raw zip bytes. This
mirrors how a real gate inspects a jar (open the zip, read each entry) and lets
the correctness property exercise the predicate directly against both synthetic
(in-memory ``zipfile``) jars and real ``nix build`` jar outputs, with no
filesystem or Nix dependency.

Design references:
    * Correctness Property 2: Built jars are real and contain no placeholder
      marker -- "the jar's manifest declares a ``Main-Class`` or plugin
      entrypoint, the jar contains at least one compiled ``.class`` entry, and
      the jar contains no ``PLACEHOLDER ARTIFACT`` marker bytes and is not
      zero-byte."
    * Components -- Per-fork Nix-wrapped Gradle build (R2): "A jar output is a
      real jar: its manifest declares a ``Main-Class`` (Lavalink server) or
      plugin entrypoint, and it contains compiled ``.class`` files -- never a
      zero-byte or ``PLACEHOLDER ARTIFACT`` marker."
    * Components -- Wire real plugin jars (R4): bundled jars "SHALL NOT contain
      any placeholder marker output (for example the ``PLACEHOLDER ARTIFACT``
      text emitted by ``mkPlaceholderJar``)."

Requirements: 2.6, 4.6
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

__all__ = [
    "MANIFEST_ENTRY",
    "PLACEHOLDER_MARKER",
    "PLUGIN_DESCRIPTOR_PREFIX",
    "JarDescriptor",
    "is_real_jar",
]

#: The manifest path inside a jar (a zip). A real jar declares its entrypoint
#: here via a ``Main-Class`` attribute.
MANIFEST_ENTRY = "META-INF/MANIFEST.MF"

#: The directory prefix under which Lavalink plugin jars carry their plugin
#: descriptor ``.properties`` entry naming the plugin class.
PLUGIN_DESCRIPTOR_PREFIX = "lavalink.plugins/"

#: The placeholder marker bytes emitted by the ``mkPlaceholderJar`` derivations
#: this migration removes. Any occurrence of this text in *any* jar entry's
#: decompressed data disqualifies the jar as real (R2.6, R4.6).
PLACEHOLDER_MARKER = b"PLACEHOLDER ARTIFACT"


@dataclass(frozen=True)
class JarDescriptor:
    """A structural view of a jar (zip) sufficient to classify it (Property 2).

    A jar is a zip archive whose entries are typically compressed, so the
    entrypoint and placeholder markers live in each entry's *decompressed*
    bytes. This descriptor therefore carries the decompressed entry map plus the
    jar's on-disk size, exactly the things the predicate reasons over -- so a
    synthetic in-memory jar and a real ``nix build`` output are described the
    same way.

    Attributes:
        entries: Mapping of zip entry name (for example
            ``META-INF/MANIFEST.MF``, ``com/example/Foo.class``) to that entry's
            **decompressed** bytes. Used to detect the manifest, compiled
            ``.class`` entries, the plugin descriptor, and any placeholder
            marker.
        size_bytes: The jar's total on-disk byte size. Used to detect a
            zero-byte jar. Defaults to ``0`` (a zero-byte jar has no entries).
    """

    entries: Mapping[str, bytes]
    size_bytes: int = 0


def _has_class_entry(entries: Mapping[str, bytes]) -> bool:
    """Return whether at least one entry is a compiled ``.class`` file."""
    return any(name.endswith(".class") for name in entries)


def _no_placeholder_marker(entries: Mapping[str, bytes]) -> bool:
    """Return whether no entry's decompressed data carries the marker (R2.6)."""
    return all(PLACEHOLDER_MARKER not in data for data in entries.values())


def _manifest_declares_main_class(entries: Mapping[str, bytes]) -> bool:
    """Return whether the manifest declares a non-empty ``Main-Class``.

    The jar manifest is a set of ``Key: Value`` lines. A real runnable jar
    (for example the Lavalink server jar) declares ``Main-Class`` with a
    non-empty value.
    """
    manifest = entries.get(MANIFEST_ENTRY)
    if manifest is None:
        return False
    manifest_text = manifest.decode("utf-8", "replace")
    for raw_line in manifest_text.splitlines():
        key, sep, value = raw_line.partition(":")
        if sep and key.strip().lower() == "main-class" and value.strip():
            return True
    return False


def _declares_plugin_entrypoint(entries: Mapping[str, bytes]) -> bool:
    """Return whether the jar declares a Lavalink plugin entrypoint.

    Lavalink plugin jars (``lavasrc-plugin``, ``youtube-plugin-sabr``) do not
    declare a ``Main-Class``; they declare their entrypoint through a plugin
    descriptor -- a ``lavalink.plugins/*.properties`` entry naming the plugin
    class via a ``plugin.class``/``pluginClass`` key. This treats a jar as
    having a plugin entrypoint when such a descriptor entry exists and names a
    non-empty plugin class.
    """
    for name, data in entries.items():
        if not (
            name.startswith(PLUGIN_DESCRIPTOR_PREFIX)
            and name.endswith(".properties")
        ):
            continue
        text = data.decode("utf-8", "replace")
        for raw_line in text.splitlines():
            key, sep, value = raw_line.partition("=")
            if sep and key.strip() in ("plugin.class", "pluginClass") and value.strip():
                return True
    return False


def is_real_jar(descriptor: JarDescriptor) -> bool:
    """Classify a jar descriptor as a real jar vs a placeholder/degenerate one.

    Implements Property 2 / R2.6, R4.6. Returns ``True`` if and only if **all**
    of the following hold:

    #. **Non-empty.** The jar has non-zero on-disk size and at least one entry.
    #. **No placeholder marker.** No jar entry's decompressed data contains the
       ``PLACEHOLDER ARTIFACT`` marker bytes.
    #. **Manifest present.** The jar contains a ``META-INF/MANIFEST.MF`` entry.
    #. **Declares an entrypoint.** The manifest declares a non-empty
       ``Main-Class`` **or** the jar declares a Lavalink plugin entrypoint
       descriptor.
    #. **Has compiled classes.** The jar contains at least one ``.class`` entry.

    Any jar that is zero-byte, carries the placeholder marker, lacks a manifest,
    declares neither a ``Main-Class`` nor a plugin entrypoint, or has no compiled
    ``.class`` entry is rejected.

    Args:
        descriptor: The structural view of the jar to classify.

    Returns:
        ``True`` if the jar is a real, runnable jar per the invariants above,
        ``False`` otherwise.

    Requirements: 2.6, 4.6
    """
    entries = descriptor.entries

    # 1. Not zero-byte (and has content to inspect).
    if descriptor.size_bytes <= 0 or not entries:
        return False

    # 2. No placeholder marker in any entry.
    if not _no_placeholder_marker(entries):
        return False

    # 3. Manifest present.
    if MANIFEST_ENTRY not in entries:
        return False

    # 4. Declares a Main-Class or a plugin entrypoint.
    if not (
        _manifest_declares_main_class(entries)
        or _declares_plugin_entrypoint(entries)
    ):
        return False

    # 5. At least one compiled .class entry.
    return _has_class_entry(entries)
