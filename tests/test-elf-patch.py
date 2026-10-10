#!/usr/bin/env python3
"""Reject unsafe ELF layouts before mutation and preserve existing LOAD bytes."""
import importlib.util
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('patch_runtime', Path(__file__).resolve().parents[1] / 'tools/patch-runtime.py')
patch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(patch)


class ElfPatchTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.binary = Path(directory.name) / 'binary'
        self.library = Path(directory.name) / 'compat.so'
        data = bytearray(0x4000)
        patch.EHDR.pack_into(data, 0, b'\x7fELF\x02\x01\x01' + bytes(9), 2, 183, 1, 0x400100, 64, 0x3000, 0, 64, 56, 3, 64, 4, 1)
        patch.PHDR.pack_into(data, 64, 1, 6, 0, 0x3fe000, 0x3fe000, 0x100, 0x100, 4096)
        patch.PHDR.pack_into(data, 120, 1, 5, 0x2000, 0x400000, 0x400000, 0x2000, 0x2000, 4096)
        patch.PHDR.pack_into(data, 176, 2, 6, 0x2600, 0x400600, 0x400600, 64, 64, 8)
        names = b'\0.shstrtab\0.dynstr\0.dynamic\0'
        strings = b'\0libc.so\0'
        data[0x2100:0x2120] = bytes(range(32))
        data[0x2400:0x2400 + len(strings)] = strings
        data[0x2800:0x2800 + len(names)] = names
        for i, entry in enumerate([(1, 1), (5, 0x400400), (10, len(strings)), (0, 0)]):
            patch.DYN.pack_into(data, 0x2600 + i * 16, *entry)
        for i, section in enumerate([(1,3,0,0,0x2800,len(names),0,0,1,0),
                                     (11,3,2,0x400400,0x2400,len(strings),0,0,1,0),
                                     (19,6,3,0x400600,0x2600,64,2,0,8,16)], 1):
            patch.SHDR.pack_into(data, 0x3000 + i * 64, *section)
        self.original = bytes(data)
        self.binary.write_bytes(data)

    def test_existing_program_bytes_and_addresses_survive(self):
        self.assertTrue(patch.add_dependency(self.binary, self.library))
        result = self.binary.read_bytes()
        self.assertEqual(len(result), len(self.original))
        self.assertEqual(result[0x1000:0x3000], self.original[0x1000:0x3000])
        self.assertEqual(result[120:176], self.original[120:176])
        dynamic = patch.PHDR.unpack_from(result, 176)
        needed = patch.DYN.unpack_from(result, dynamic[2])
        self.assertEqual(needed, (1, 9))
        strings = patch.SHDR.unpack_from(result, 0x3080)
        self.assertEqual(result[strings[4] + needed[1]:strings[4] + strings[5]], str(self.library).encode() + b'\0')
        self.assertFalse(patch.add_dependency(self.binary, self.library))
        self.assertEqual(self.binary.read_bytes(), result)

    def test_dirty_gap_is_not_overwritten(self):
        data = bytearray(self.original)
        data[0x102] = 42
        self.binary.write_bytes(data)
        with self.assertRaisesRegex(RuntimeError, 'safe zero-filled'):
            patch.add_dependency(self.binary, self.library)
        self.assertEqual(self.binary.read_bytes(), data)

    def test_other_architecture_is_not_modified(self):
        data = bytearray(self.original)
        data[18] = 62
        self.binary.write_bytes(data)
        with self.assertRaisesRegex(RuntimeError, 'AArch64'):
            patch.add_dependency(self.binary, self.library)
        self.assertEqual(self.binary.read_bytes(), data)

    def test_relative_dependency_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, 'absolute path'):
            patch.add_dependency(self.binary, Path('compat.so'))
        self.assertEqual(self.binary.read_bytes(), self.original)


if __name__ == '__main__':
    unittest.main()
