#!/usr/bin/env python3
"""Inventory instruction candidates in the pinned ELF, without executing it.

Every aligned word of every file-backed executable PT_LOAD is scanned. PLT
names are resolved from decoded AArch64 stubs and ELF JUMP_SLOT relocations,
independently of Ghidra's recovered function/reference tables.
"""
import argparse
from collections import Counter
import hashlib
import json
import mmap
from pathlib import Path
import struct

ORIGINAL = 'b0bbc936a82930933d7d5645081050d5f827a946cf6abc6575383b777a0feec0'
PATCHED = '0563350c19d156e3c4e1595f9524185de7d948543ac5edbd5c6b9140e376a06d'
TEXT = '270a16c4fb9a0db57546bc5b0cbdf721e59285f16a8f546ffba827fb82842f00'
GROUPS = {
    'network': {'socket', 'socketpair', 'connect', 'bind', 'listen', 'accept', 'accept4',
                'send', 'sendto', 'sendmsg', 'sendmmsg', 'recv', 'recvfrom', 'recvmsg',
                'recvmmsg', 'shutdown', 'getsockopt', 'setsockopt', 'getaddrinfo',
                'freeaddrinfo', 'getnameinfo'},
    'fd_and_ipc': {'read', 'readv', 'pread', 'preadv', 'write', 'writev', 'pwrite',
                   'pwritev', 'sendfile', 'splice', 'tee', 'vmsplice', 'copy_file_range',
                   'open', 'openat', 'close', 'dup', 'dup2', 'dup3', 'fcntl', 'ioctl',
                   'pipe', 'pipe2', 'fopen', 'fdopen', 'fread', 'fwrite'},
    'execution_and_loading': {'syscall', 'fork', 'vfork', 'clone', 'execve', 'execvp',
                              'execvpe', 'posix_spawn', 'posix_spawnp', 'pidfd_spawnp',
                              'system', 'popen', 'dlopen', 'dlsym', 'mmap', 'mprotect'},
}


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def signed(value, bits):
    return value - (1 << bits) if value & (1 << (bits - 1)) else value


def scan(binary):
    with binary.open('rb') as stream:
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
    require(digest in (ORIGINAL, PATCHED), 'Unreviewed binary hash')
    with binary.open('rb') as stream, mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ) as data:
        header = struct.unpack_from('<16sHHIQQQIHHHHHH', data)
        require(header[0][:6] == b'\x7fELF\x02\x01' and header[1:3] == (2, 183), 'Expected AArch64 ET_EXEC')
        require(header[9] == 56 and header[11] == 64, 'Unexpected ELF table strides')
        sh = [struct.unpack_from('<IIQQQQIIQQ', data, header[6] + i * 64) for i in range(header[12])]
        names_row = sh[header[13]]
        names = data[names_row[4]:names_row[4] + names_row[5]]
        sections = {names[row[0]:].split(b'\0', 1)[0].decode(): row for row in sh}

        def section_bytes(name):
            row = sections[name]
            return data[row[4]:row[4] + row[5]]

        require(hashlib.sha256(section_bytes('.text')).hexdigest() == TEXT, 'Unexpected .text')
        dynstr = section_bytes('.dynstr')
        symbols = []
        for name, info, other, index, value, size in struct.iter_unpack('<IBBHQQ', section_bytes('.dynsym')):
            symbols.append({'name': dynstr[name:].split(b'\0', 1)[0].decode(),
                            'index': index, 'value': value, 'type': info & 15})
        relocations = {}
        for address, info, addend in struct.iter_unpack('<QQq', section_bytes('.rela.plt')):
            require(info & 0xffffffff == 1026 and addend == 0, 'Unexpected PLT relocation')
            relocations[address] = symbols[info >> 32]['name']
        plt, plt_base = section_bytes('.plt'), sections['.plt'][3]
        stubs = {}
        for offset in range(32, len(plt), 16):
            address = plt_base + offset
            adrp, ldr, add, branch = struct.unpack_from('<IIII', plt, offset)
            require(adrp & 0x9f00001f == 0x90000010 and ldr & 0xffc003ff == 0xf9400211
                    and add & 0xffc003ff == 0x91000210 and branch == 0xd61f0220,
                    'Unexpected PLT stub at ' + hex(address))
            page = (address & ~4095) + (signed(((adrp >> 29) & 3) | (((adrp >> 5) & 0x7ffff) << 2), 21) << 12)
            displacement = ((ldr >> 10) & 0xfff) * 8
            require(displacement == ((add >> 10) & 0xfff), 'Inconsistent PLT GOT offsets')
            got = page + displacement
            require(got in relocations, 'PLT GOT slot has no JUMP_SLOT relocation')
            stubs[address] = {'name': relocations[got], 'got': hex(got), 'bytes': plt[offset:offset + 16].hex()}
        require(len(stubs) == len(relocations), 'Incomplete PLT mapping')
        selected = set().union(*GROUPS.values())
        counts, branches, svc, indirect, segments = Counter(), [], [], Counter(), []
        plt_end = plt_base + len(plt)
        for i in range(header[10]):
            kind, flags, offset, address, physical, file_size, mem_size, align = struct.unpack_from('<IIQQQQQQ', data, header[5] + i * 56)
            if kind != 1 or not flags & 1:
                continue
            require(address % 4 == 0 and file_size % 4 == 0, 'Unaligned executable segment')
            code = data[offset:offset + file_size]
            segments.append({'address': hex(address), 'file_offset': offset, 'file_bytes': file_size,
                             'memory_bytes': mem_size, 'sha256': hashlib.sha256(code).hexdigest(),
                             'aligned_words_scanned': file_size // 4})
            for index, (word,) in enumerate(struct.iter_unpack('<I', code)):
                pc = address + index * 4
                if word & 0x7c000000 == 0x14000000:
                    destination = pc + signed(word & 0x3ffffff, 26) * 4
                    if destination in stubs:
                        name = stubs[destination]['name']
                        counts[name] += 1
                        if name in selected:
                            branches.append({'address': hex(pc), 'kind': 'BL' if word >> 31 else 'B',
                                             'target': hex(destination), 'import': name,
                                             'bytes': struct.pack('<I', word).hex()})
                if word & 0xffe0001f == 0xd4000001:
                    svc.append({'address': hex(pc), 'immediate': (word >> 5) & 0xffff,
                                'bytes': struct.pack('<I', word).hex()})
                masked = word & 0xfffffc1f
                if masked in (0xd61f0000, 0xd63f0000):
                    indirect[('BR' if masked == 0xd61f0000 else 'BLR') + ('_inside_plt' if plt_base <= pc < plt_end else '_outside_plt')] += 1
        imports = sorted({symbol['name'] for symbol in symbols if symbol['index'] == 0 and symbol['name']})
        return {
            'schema': 1, 'claude': '2.1.296', 'original_sha256': ORIGINAL, 'scanned_sha256': digest,
            'text_sha256': TEXT, 'target_executed': False, 'segments': segments,
            'executable_file_bytes_scanned': sum(row['file_bytes'] for row in segments),
            'executable_memory_bytes_without_file': sum(row['memory_bytes'] - row['file_bytes'] for row in segments),
            'undefined_dynamic_symbols': imports,
            'plt_entries_decoded_and_relocation_matched': len(stubs),
            'direct_plt_branch_counts': dict(sorted(counts.items())),
            'selected_imports': {group: {name: {'present': name in imports, 'direct_branches': counts[name]}
                                       for name in sorted(names)} for group, names in GROUPS.items()},
            'selected_direct_branches': branches, 'svc_candidates': svc,
            'register_branch_candidate_counts': dict(indirect),
            'selected_plt_entries': {hex(address): value for address, value in stubs.items() if value['name'] in selected},
            'limits': [
                'Aligned instruction patterns are candidates; embedded data can match and reachability is not established.',
                'Completeness applies to this byte scan and decoded PLT table, not all network behavior.',
                'Imported syscall and inline SVC require argument/dataflow analysis to determine the operation.',
                'Indirect calls through GOT/function pointers and register branches are not resolved by this scan.',
                'JIT-generated code, loaded libraries, injected compatibility libraries, child processes and external helpers require separate inventories.',
                'A write/send candidate is not proof that its descriptor is a network socket or that its data is telemetry.',
            ],
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binary', type=Path, default=Path.home() / '.local/share/claude-termux/current/runtime/claude')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    require(not args.output.exists(), 'Use a new output file')
    result = scan(args.binary)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps({'executable_bytes': result['executable_file_bytes_scanned'],
                      'plt_entries': result['plt_entries_decoded_and_relocation_matched'],
                      'selected_direct_branches': len(result['selected_direct_branches']),
                      'svc_candidates': len(result['svc_candidates']),
                      'register_branch_candidates': result['register_branch_candidate_counts']}))


if __name__ == '__main__':
    main()
