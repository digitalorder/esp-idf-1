#!/usr/bin/env python
#
# ESP32 core dump Utility

import sys
import os
import argparse
import subprocess
import tempfile
import struct
import array
import errno

try:
    import esptool
except ImportError:
    idf_path = os.getenv('IDF_PATH')
    if idf_path is None:
        print "Esptool is not found! Install it or set proper $IDF_PATH in environment."
        sys.exit(2)
    sys.path.append('%s/components/esptool_py/esptool' % idf_path)
    import esptool

__version__ = "0.1-dev"

ESP32_COREDUMP_HDR_FMT              = '<4L'
ESP32_COREDUMP_FLASH_MAGIC_START    = 0xDEADBEEF
ESP32_COREDUMP_FLASH_MAGIC_END      = 0xACDCFEED


class Struct(object):
    def __init__(self, buf=None):
        if buf is None:
            buf = b'\0' * self.sizeof()
        fields = struct.unpack(self.__class__.fmt, buf[:self.sizeof()])
        self.__dict__.update(zip(self.__class__.fields, fields))

    def sizeof(self):
        return struct.calcsize(self.__class__.fmt)

    def dump(self):
        keys =  self.__class__.fields
        if sys.version_info > (3, 0):
            # Convert strings into bytearrays if this is Python 3
            for k in keys:
                if type(self.__dict__[k]) is str:
                    self.__dict__[k] = bytearray(self.__dict__[k], encoding='ascii')
        return struct.pack(self.__class__.fmt, *(self.__dict__[k] for k in keys))

    def __str__(self):
        keys =  self.__class__.fields
        return (self.__class__.__name__ + "({" +
            ", ".join("%s:%r" % (k, self.__dict__[k]) for k in keys) +
            "})")


class Elf32FileHeader(Struct):
    """ELF32 File header"""
    fields = ("e_ident",
              "e_type",
              "e_machine",
              "e_version",
              "e_entry",
              "e_phoff",
              "e_shoff",
              "e_flags",
              "e_ehsize",
              "e_phentsize",
              "e_phnum",
              "e_shentsize",
              "e_shnum",
              "e_shstrndx")
    fmt = "<16sHHLLLLLHHHHHH"

    def __init__(self, buf=None):
        super(Elf32FileHeader, self).__init__(buf)
        if buf is None:
            # Fill in sane ELF header for LSB32
            self.e_ident = "\x7fELF\1\1\1\0\0\0\0\0\0\0\0\0"
            self.e_version = ESPCoreDumpFile.EV_CURRENT
            self.e_ehsize = self.sizeof()


class Elf32ProgramHeader(Struct):
  """ELF32 Program Header"""
  fields = ("p_type",
            "p_offset",
            "p_vaddr",
            "p_paddr",
            "p_filesz",
            "p_memsz",
            "p_flags",
            "p_align")
  fmt = "<LLLLLLLL"


class Elf32NoteDesc(object):
    """ELF32 Note Descriptor"""
    def __init__(self, name, type, data):
        self.name = bytearray(name, encoding='ascii') + b'\0'
        self.type = type
        self.data = data

    def dump(self):
        """Conveninece function to format a note descriptor.
           All note descriptors must be concatenated and added to a
           PT_NOTE segment."""
        header = struct.pack("<LLL", len(self.name), len(self.data), self.type)
        # pad up to 4 byte alignment
        name = self.name + ((4 - len(self.name)) % 4) * b'\0'
        desc = self.data + ((4 - len(self.data)) % 4) * b'\0'
        print "dump %d %d %d %d %d" % (len(header), len(name), len(self.name), len(desc), len(self.data))
        return header + name + desc


class XtensaPrStatus(Struct):
    """Xtensa Program Status structure"""
    # Only pr_cursig and pr_pid are read by bfd
    # Structure followed by 72 bytes representing general-purpose registers
    # check elf32-xtensa.c in libbfd for details
    fields = ("si_signo", "si_code", "si_errno",
              "pr_cursig", # Current signal
              "pr_pad0",
              "pr_sigpend",
              "pr_sighold",
              "pr_pid", # LWP ID
              "pr_ppid",
              "pr_pgrp",
              "pr_sid",
              "pr_utime",
              "pr_stime",
              "pr_cutime",
              "pr_cstime")
    fmt = "<3LHHLLLLLLQQQQ"


class ESPCoreDumpSegment(esptool.ImageSegment):
    """ Wrapper class for a program segment in an ELF image, has a section
    name as well as the common properties of an ImageSegment. """
    # segment flags
    PF_X = 0x1 # Execute
    PF_W = 0x2 # Write
    PF_R = 0x4 # Read

    def __init__(self, addr, data, type, flags):
        super(ESPCoreDumpSegment, self).__init__(addr, data)
        self.flags = flags
        self.type = type

    def __repr__(self):
        return "%s %s %s" % (self.type, self.attr_str(), super(ESPCoreDumpSegment, self).__repr__())

    def attr_str(self):
        str = ''
        if self.flags & self.PF_R:
            str += 'R'
        else:
            str += ' '
        if self.flags & self.PF_W:
            str += 'W'
        else:
            str += ' '
        if self.flags & self.PF_X:
            str += 'X'
        else:
            str += ' '
        return str


class ESPCoreDumpSection(esptool.ELFSection):
    """
    TBD
    """
    # section flags
    SHF_WRITE       = 0x1
    SHF_ALLOC       = 0x2
    SHF_EXECINSTR   = 0x4

    def __init__(self, name, addr, data, flags):
        super(ESPCoreDumpSection, self).__init__(name, addr, data)
        self.flags = flags

    def __repr__(self):
        return "%s %s" % (super(ESPCoreDumpSection, self).__repr__(), self.attr_str())

    def attr_str(self):
        str = "R"
        if self.flags & self.SHF_WRITE:
            str += 'W'
        else:
            str += ' '
        if self.flags & self.SHF_EXECINSTR:
            str += 'X'
        else:
            str += ' '
        if self.flags & self.SHF_ALLOC:
            str += 'A'
        else:
            str += ' '
        return str


class ESPCoreDumpFile(esptool.ELFFile):
    # ELF file type
    ET_NONE             = 0x0 # No file type
    ET_REL              = 0x1 # Relocatable file
    ET_EXEC             = 0x2 # Executable file
    ET_DYN              = 0x3 # Shared object file
    ET_CORE             = 0x4 # Core file
    # ELF file version
    EV_NONE             = 0x0
    EV_CURRENT          = 0x1
    # ELF file machine type
    EM_NONE             = 0x0
    EM_XTENSA           = 0x5E
    # section types
    SEC_TYPE_PROGBITS   = 0x01
    SEC_TYPE_STRTAB     = 0x03
    # special section index
    SHN_UNDEF           = 0x0
    # program segment types
    PT_NULL             = 0x0
    PT_LOAD             = 0x1
    PT_DYNAMIC          = 0x2
    PT_INTERP           = 0x3
    PT_NOTE             = 0x4
    PT_SHLIB            = 0x5
    PT_PHDR             = 0x6

    def __init__(self, name=None):
        if name:
            super(ESPCoreDumpFile, self).__init__(name)
        else:
            self.sections = []
            self.program_segments = []
            self.e_type = self.ET_NONE
            self.e_machine = self.EM_NONE

    def _read_elf_file(self, f):
        # read the ELF file header
        LEN_FILE_HEADER = 0x34
        try:
            (ident,type,machine,_version,
             self.entrypoint,phoff,shoff,_flags,
             _ehsize, phentsize,phnum,_shentsize,
             shnum,shstrndx) = struct.unpack("<16sHHLLLLLHHHHHH", f.read(LEN_FILE_HEADER))
        except struct.error as e:
            raise FatalError("Failed to read a valid ELF header from %s: %s" % (self.name, e))

        if ident[0] != '\x7f' or ident[1:4] != 'ELF':
            raise FatalError("%s has invalid ELF magic header" % self.name)
        if machine != self.EM_XTENSA:
            raise FatalError("%s does not appear to be an Xtensa ELF file. e_machine=%04x" % (self.name, machine))
        self.e_type = type
        self.e_machine = machine
        if shnum > 0:
            self._read_sections(f, shoff, shstrndx)
        else:
            self.sections = []
            if phnum > 0:
                self._read_program_segments(f, phoff, phentsize, phnum)
            else:
                self.program_segments = []

    def _read_sections(self, f, section_header_offs, shstrndx):
        f.seek(section_header_offs)
        section_header = f.read()
        LEN_SEC_HEADER = 0x28
        if len(section_header) == 0:
            raise FatalError("No section header found at offset %04x in ELF file." % section_header_offs)
        if len(section_header) % LEN_SEC_HEADER != 0:
            print 'WARNING: Unexpected ELF section header length %04x is not mod-%02x' % (len(section_header),LEN_SEC_HEADER)

        # walk through the section header and extract all sections
        section_header_offsets = range(0, len(section_header), LEN_SEC_HEADER)

        def read_section_header(offs):
            name_offs,sec_type,flags,lma,sec_offs,size = struct.unpack_from("<LLLLLL", section_header[offs:])
            return (name_offs, sec_type, flags, lma, size, sec_offs)
        all_sections = [read_section_header(offs) for offs in section_header_offsets]
        prog_sections = [s for s in all_sections if s[1] == esptool.ELFFile.SEC_TYPE_PROGBITS]

        # search for the string table section
        if not shstrndx * LEN_SEC_HEADER in section_header_offsets:
            raise FatalError("ELF file has no STRTAB section at shstrndx %d" % shstrndx)
        _,sec_type,_,_,sec_size,sec_offs = read_section_header(shstrndx * LEN_SEC_HEADER)
        if sec_type != esptool.ELFFile.SEC_TYPE_STRTAB:
            print 'WARNING: ELF file has incorrect STRTAB section type 0x%02x' % sec_type
        f.seek(sec_offs)
        string_table = f.read(sec_size)

        # build the real list of ELFSections by reading the actual section names from the
        # string table section, and actual data for each section from the ELF file itself
        def lookup_string(offs):
            raw = string_table[offs:]
            return raw[:raw.index('\x00')]

        def read_data(offs,size):
            f.seek(offs)
            return f.read(size)

        prog_sections = [ESPCoreDumpSection(lookup_string(n_offs), lma, read_data(offs, size), flags) for (n_offs, _type, flags, lma, size, offs) in prog_sections
                         if lma != 0]
        self.sections = prog_sections

    def _read_program_segments(self, f, seg_table_offs, entsz, num):
        f.seek(seg_table_offs)
        seg_table = f.read(entsz*num)
        LEN_SEG_HEADER = 0x20
        if len(seg_table) == 0:
            raise FatalError("No program header table found at offset %04x in ELF file." % seg_table_offs)
        if len(seg_table) % LEN_SEG_HEADER != 0:
            print 'WARNING: Unexpected ELF program header table length %04x is not mod-%02x' % (len(seg_table),LEN_SEG_HEADER)

        # walk through the program segment table and extract all segments
        seg_table_offs = range(0, len(seg_table), LEN_SEG_HEADER)

        def read_program_header(offs):
            type,offset,vaddr,_paddr,filesz,_memsz,_flags,_align = struct.unpack_from("<LLLLLLLL", seg_table[offs:])
            return (type,offset,vaddr,filesz)
        all_segments = [read_program_header(offs) for offs in seg_table_offs]
        prog_segments = [s for s in all_segments if s[0] == self.PT_LOAD]

        # build the real list of ImageSegment by reading actual data for each segment from the ELF file itself
        def read_data(offs,size):
            f.seek(offs)
            return f.read(size)

        prog_segments = [esptool.ImageSegment(vaddr, read_data(offset, filesz), offset) for (_type, offset, vaddr, filesz) in prog_segments
                         if vaddr != 0]
        self.program_segments = prog_segments
#         print "prog_segments=%s" % (self.program_segments)

    # currently merging is not supported
    def add_program_segment(self, addr, data, type, flags):
        data_sz = len(data)
        print "add_program_segment: %x %d" % (addr, data_sz)
        # check for overlapping and merge if needed
        if addr != 0 and data_sz != 0:
            for ps in self.program_segments:
                seg_len = len(ps.data)
                if addr >= ps.addr and addr < (ps.addr + seg_len):
                    raise FatalError("Can not add overlapping region [%x..%x] to ELF file. Conflict with existing [%x..%x]." % 
                                     (addr, addr + data_sz - 1, ps.addr, ps.addr + seg_len - 1))
                if (addr + data_sz) > ps.addr and (addr + data_sz) <= (ps.addr + seg_len):
                    raise FatalError("Can not add overlapping region [%x..%x] to ELF file. Conflict with existing [%x..%x]." % 
                                     (addr, addr + data_sz - 1, ps.addr, ps.addr + seg_len - 1))
        # append
        self.program_segments.append(ESPCoreDumpSegment(addr, data, type, flags))

    # currently dumps only program segments.
    # dumping sections is not supported yet
    def dump(self, f):
        print "dump to '%s'" % f
        # write ELF header
        ehdr = Elf32FileHeader()
        ehdr.e_type = self.e_type
        ehdr.e_machine = self.e_machine
        ehdr.e_entry = 0
        ehdr.e_phoff = ehdr.sizeof()
        ehdr.e_shoff = 0
        ehdr.e_flags = 0
        ehdr.e_phentsize = Elf32ProgramHeader().sizeof()
        ehdr.e_phnum = len(self.program_segments)
        ehdr.e_shentsize = 0
        ehdr.e_shnum = 0
        ehdr.e_shstrndx = self.SHN_UNDEF
        f.write(ehdr.dump())
        # write program header table
        cur_off = ehdr.e_ehsize + ehdr.e_phnum * ehdr.e_phentsize
#         print "" % (ehdr.e_ehsize, ehdr.e_phnum, ehdr.e_phentsize)
        for i in range(len(self.program_segments)):
            print "dump header for seg '%s'" % self.program_segments[i]
            phdr = Elf32ProgramHeader()
            phdr.p_type = self.program_segments[i].type
            phdr.p_offset = cur_off
            phdr.p_vaddr = self.program_segments[i].addr
            phdr.p_paddr = phdr.p_vaddr # TODO
            phdr.p_filesz = len(self.program_segments[i].data)
            phdr.p_memsz = phdr.p_filesz # TODO
            phdr.p_flags = self.program_segments[i].flags
            phdr.p_align = 0 # TODO
#             print "header '%s'" % phdr
            f.write(phdr.dump())
            cur_off += phdr.p_filesz
        # write program segments
        for i in range(len(self.program_segments)):
            print "dump seg '%s'" % self.program_segments[i]
            f.write(self.program_segments[i].data)


class ESPCoreDumpError(RuntimeError):
    """
    TBD
    """
    def __init__(self, message):
        super(ESPCoreDumpError, self).__init__(message)


class ESPCoreDumpLoaderError(ESPCoreDumpError):
    """
    TBD
    """
    def __init__(self, message):
        super(ESPCoreDumpLoaderError, self).__init__(message)


class ESPCoreDumpLoader(object):
    """
    TBD
    """
    FLASH_READ_BLOCK_SZ = 0x2000
    def __init__(self, off, path=None, chip='esp32', port=None, baud=None):
#         print "esptool.__file__ %s" % esptool.__file__
        if not path:
            self.path = esptool.__file__
            self.path = self.path[:-1]
        else:
            self.path =  path
        self.port = port
        self.baud = baud
        self.chip = chip
        self.fcores = []
        self.fgdbcore = None 
        self._load_coredump(off)
        
    def _load_coredump(self, off):
        args = [self.path, '-c', self.chip]
        if self.port:
            args.extend(['-p', self.port])
        if self.baud:
            args.extend(['-b', str(self.baud)])
        read_sz = self.FLASH_READ_BLOCK_SZ
        read_off = off
        args.extend(['read_flash', str(read_off), str(read_sz), ''])
        try:
            dump_sz = 0
            tot_len = 0
            while True:
                fhnd,fname = tempfile.mkstemp()
#                 print "tmpname %s" % fname
#                 os.close(fhnd)
                args[-1] = fname
                et_out = subprocess.check_output(args)
                print et_out
    #             data = os.fdopen(fhnd, 'r').read(sz)
                self.fcores.append(os.fdopen(fhnd, 'r'))
                if dump_sz == 0:
                    # read dump length from the first block
                    dump_sz = self._read_core_dump_length(self.fcores[0])
                tot_len += read_sz
                if tot_len >= dump_sz:
                    break
                read_off += read_sz
                if dump_sz - tot_len >= self.FLASH_READ_BLOCK_SZ:
                    read_sz = self.FLASH_READ_BLOCK_SZ
                else:
                    read_sz = dump_sz - tot_len
                args[-3] = str(read_off)
                args[-2] = str(read_sz)
                
        except subprocess.CalledProcessError as e: 
            print "esptool script execution failed with err %d" % e.returncode
            print "Command ran: '%s'" % e.cmd
            print "Command out:"
            print e.output
            self.cleanup()
            return []

    def _read_core_dump_length(self, f):
        global ESP32_COREDUMP_HDR_FMT
        global ESP32_COREDUMP_FLASH_MAGIC_START
        print "Read core dump header from '%s'" % f.name
        data = f.read(4*4)
        mag1,tot_len,task_num,tcbsz = struct.unpack_from(ESP32_COREDUMP_HDR_FMT, data)
        if mag1 != ESP32_COREDUMP_FLASH_MAGIC_START:
            raise ESPCoreDumpLoaderError("Invalid start magic number!")
        return tot_len
                
    def remove_tmp_file(self, fname):
        try:
            os.remove(fname)
        except OSError as e:
            if e.errno != errno.ENOENT:
                print "Warning failed to remove temp file '%s'!" % fname

    def _get_registers_from_stack(self, data, grows_down):
    # from "gdb/xtensa-tdep.h"
    # typedef struct
    # {
    #0    xtensa_elf_greg_t pc;
    #1    xtensa_elf_greg_t ps;
    #2    xtensa_elf_greg_t lbeg;
    #3    xtensa_elf_greg_t lend;
    #4    xtensa_elf_greg_t lcount;
    #5    xtensa_elf_greg_t sar;
    #6    xtensa_elf_greg_t windowstart;
    #7    xtensa_elf_greg_t windowbase;
    #8..63 xtensa_elf_greg_t reserved[8+48];
    #64   xtensa_elf_greg_t ar[64];
    # } xtensa_elf_gregset_t;
        REG_PC_IDX=0
        REG_PS_IDX=1
        REG_LB_IDX=2
        REG_LE_IDX=3
        REG_LC_IDX=4
        REG_SAR_IDX=5
        REG_WS_IDX=6
        REG_WB_IDX=7
        REG_AR_START_IDX=64
        REG_AR_NUM=64
        # FIXME: acc to xtensa_elf_gregset_t number of regs must be 128, 
        # but gdb complanis when it less then 129
        REG_NUM=129
     
        XT_SOL_EXIT=0
        XT_SOL_PC=1
        XT_SOL_PS=2
        XT_SOL_NEXT=3
        XT_SOL_AR_START=4
        XT_SOL_AR_NUM=4
        XT_SOL_FRMSZ=8
     
        XT_STK_EXIT=0
        XT_STK_PC=1
        XT_STK_PS=2
        XT_STK_AR_START=3
        XT_STK_AR_NUM=16
        XT_STK_SAR=19
        XT_STK_EXCCAUSE=20
        XT_STK_EXCVADDR=21
        XT_STK_LBEG=22
        XT_STK_LEND=23
        XT_STK_LCOUNT=24
        XT_STK_FRMSZ=25
         
        regs = [0] * REG_NUM
        # TODO: support for growing up stacks
        if not grows_down:
            print "Growing up stacks are not supported for now!"
            return regs
    #     for i in range(REG_NUM):
    #         regs[i] = i
    #     return regs
        ex_struct = "<%dL" % XT_STK_FRMSZ
        if len(data) < struct.calcsize(ex_struct):
            print "Too small stack to keep frame: %d bytes!" % len(data)
            return regs
    
        stack = struct.unpack(ex_struct, data[:struct.calcsize(ex_struct)])
        # Stack frame type indicator is always the first item
        rc = stack[XT_STK_EXIT]
        if rc != 0:
            print "EXCSTACKFRAME %d" % rc
            regs[REG_PC_IDX] = stack[XT_STK_PC]
            regs[REG_PS_IDX] = stack[XT_STK_PS]
            for i in range(XT_STK_AR_NUM):
                regs[REG_AR_START_IDX + i] = stack[XT_STK_AR_START + i]
            regs[REG_SAR_IDX] = stack[XT_STK_SAR]
            regs[REG_LB_IDX] = stack[XT_STK_LBEG]
            regs[REG_LE_IDX] = stack[XT_STK_LEND]
            regs[REG_LC_IDX] = stack[XT_STK_LCOUNT]
            print "get_registers_from_stack: pc %x ps %x a0 %x a1 %x a2 %x a3 %x" % (
                regs[REG_PC_IDX], regs[REG_PS_IDX], regs[REG_AR_NUM + 0],
                regs[REG_AR_NUM + 1], regs[REG_AR_NUM + 2], regs[REG_AR_NUM + 3])
        else:
            print "SOLSTACKFRAME %d" % rc
            regs[REG_PC_IDX] = stack[XT_SOL_PC]
            regs[REG_PS_IDX] = stack[XT_SOL_PS]
            for i in range(XT_SOL_AR_NUM):
                regs[REG_AR_START_IDX + i] = stack[XT_SOL_AR_START + i]
            nxt = stack[XT_SOL_NEXT]
            print "get_registers_from_stack: pc %x ps %x a0 %x a1 %x a2 %x a3 %x" % (
                regs[REG_PC_IDX], regs[REG_PS_IDX], regs[REG_AR_NUM + 0],
                regs[REG_AR_NUM + 1], regs[REG_AR_NUM + 2], regs[REG_AR_NUM + 3])
             
        # TODO: remove magic hack with saved PC to get proper value
        regs[REG_PC_IDX] = ((regs[REG_PC_IDX] & 0x3FFFFFFF) | 0x40000000)
        if regs[REG_PC_IDX] & 0x80000000:
            regs[REG_PC_IDX] = (regs[REG_PC_IDX] & 0x3fffffff) | 0x40000000;
        if regs[REG_AR_START_IDX + 0] & 0x80000000:
            regs[REG_AR_START_IDX + 0] = (regs[REG_AR_START_IDX + 0] & 0x3fffffff) | 0x40000000;
        return regs

    def cleanup(self):
#         if self.fgdbcore:
#             self.fgdbcore.close()
#            self.remove_tmp_file(self.fgdbcore.name)
        for f in self.fcores:
            if f:
                f.close()
                self.remove_tmp_file(f.name)

    def get_corefile_from_flash(self):
        """ TBD
        """
        global ESP32_COREDUMP_HDR_FMT
        ESP32_COREDUMP_HDR_SZ = struct.calcsize(ESP32_COREDUMP_HDR_FMT)
        ESP32_COREDUMP_TSK_HDR_FMT = '<LLL'
        ESP32_COREDUMP_TSK_HDR_SZ = struct.calcsize(ESP32_COREDUMP_TSK_HDR_FMT)
        ESP32_COREDUMP_MAGIC_FMT = '<L'
        ESP32_COREDUMP_MAGIC_SZ = struct.calcsize(ESP32_COREDUMP_MAGIC_FMT)
        no_progress = True #False
        if no_progress:
            flash_progress = None
        else:
            def flash_progress(progress, length):
                msg = '%d (%d %%)' % (progress, progress * 100.0 / length)
                padding = '\b' * len(msg)
                if progress == length:
                    padding = '\n'
                sys.stdout.write(msg + padding)
                sys.stdout.flush()
    
        core_off = 0
        print "Read core dump header"
        data = self.read_flash(core_off, ESP32_COREDUMP_HDR_SZ, flash_progress)
        mag1,tot_len,task_num,tcbsz = struct.unpack_from(ESP32_COREDUMP_HDR_FMT, data)
        tcbsz_aligned = tcbsz
        if tcbsz_aligned % 4:
            tcbsz_aligned = 4*(tcbsz_aligned/4 + 1)
        print "mag1=%x, tot_len=%d, task_num=%d, tcbsz=%d" % (mag1,tot_len,task_num,tcbsz)
        core_off += ESP32_COREDUMP_HDR_SZ
        core_elf = ESPCoreDumpFile()
        notes = b''
        for i in range(task_num):
            print "Read task[%d] header" % i
            data = self.read_flash(core_off, ESP32_COREDUMP_TSK_HDR_SZ, flash_progress)
            tcb_addr,stack_top,stack_end = struct.unpack_from(ESP32_COREDUMP_TSK_HDR_FMT, data)
            if stack_end > stack_top:
                stack_len = stack_end - stack_top
                stack_base = stack_top
            else:
                stack_len = stack_top - stack_end
                stack_base = stack_end
            print "tcb_addr=%x, stack_top=%x, stack_end=%x, stack_len=%d" % (tcb_addr,stack_top,stack_end,stack_len)
    
            stack_len_aligned = stack_len
            if stack_len_aligned % 4:
                stack_len_aligned = 4*(stack_len_aligned/4 + 1)
                
            core_off += ESP32_COREDUMP_TSK_HDR_SZ
            print "Read task[%d] TCB" % i
            data = self.read_flash(core_off, tcbsz_aligned, flash_progress)
            if tcbsz != tcbsz_aligned:
                core_elf.add_program_segment(tcb_addr, data[:tcbsz - tcbsz_aligned], ESPCoreDumpFile.PT_LOAD, ESPCoreDumpSegment.PF_R | ESPCoreDumpSegment.PF_W)
            else:
                core_elf.add_program_segment(tcb_addr, data, ESPCoreDumpFile.PT_LOAD, ESPCoreDumpSegment.PF_R | ESPCoreDumpSegment.PF_W)
    #         print "tcb=%s" % data
            core_off += tcbsz_aligned
            print "Read task[%d] stack %d bytes" % (i,stack_len)
            data = self.read_flash(core_off, stack_len_aligned, flash_progress)
    #         print "stk=%s" % data
            if stack_len != stack_len_aligned:
                data = data[:stack_len - stack_len_aligned]
            core_elf.add_program_segment(stack_base, data, ESPCoreDumpFile.PT_LOAD, ESPCoreDumpSegment.PF_R | ESPCoreDumpSegment.PF_W)
            core_off += stack_len_aligned
    
            task_regs = self._get_registers_from_stack(data, stack_end > stack_top)
            prstatus = XtensaPrStatus()
            prstatus.pr_cursig = 0 # TODO: set sig only for current/failed task
            prstatus.pr_pid = i # TODO: use pid assigned by OS
            note = Elf32NoteDesc("CORE", 1, prstatus.dump() + struct.pack("<%dL" % len(task_regs), *task_regs)).dump()
            print "NOTE_LEN %d" % len(note)
            notes += note
    
        print "Read core dump endmarker"
        data = self.read_flash(core_off, ESP32_COREDUMP_MAGIC_SZ, flash_progress)
        mag = struct.unpack_from(ESP32_COREDUMP_MAGIC_FMT, data)
        print "mag2=%x" % (mag)
    
        # add notes
        core_elf.add_program_segment(0, notes, ESPCoreDumpFile.PT_NOTE, 0)
    
        core_elf.e_type = ESPCoreDumpFile.ET_CORE
        core_elf.e_machine = ESPCoreDumpFile.EM_XTENSA
        fhnd,fname = tempfile.mkstemp()
        self.fgdbcore = os.fdopen(fhnd, 'wb')
        core_elf.dump(self.fgdbcore)
        return fname
    ######################### END ###########################

    def read_flash(self, off, sz, progress=None):
#         print "read_flash: %x %d" % (off, sz)
        id = off / self.FLASH_READ_BLOCK_SZ
        if id >= len(self.fcores):
            return ''
        self.fcores[id].seek(off % self.FLASH_READ_BLOCK_SZ)
        data = self.fcores[id].read(sz)
#         print "data1: %s" % data
        return data

class GDBMIOutRecordHandler(object):
    """ TBD
    """
    TAG = ''

    def __init__(self, f, verbose=False):
        self.verbose = verbose

    def execute(self, ln):
        if self.verbose:
            print "%s.execute '%s'" % (self.__class__.__name__, ln)


class GDBMIOutStreamHandler(GDBMIOutRecordHandler):
    """ TBD
    """
    def __init__(self, f, verbose=False):
        super(GDBMIOutStreamHandler, self).__init__(None, verbose)
        self.func = f

    def execute(self, ln):
        GDBMIOutRecordHandler.execute(self, ln)
        if self.func:
            # remove TAG / quotes and replace c-string \n with actual NL
            self.func(ln[1:].strip('"').replace('\\n', '\n').replace('\\t', '\t'))


class GDBMIResultHandler(GDBMIOutRecordHandler):
    """ TBD
    """
    TAG = '^'
    RC_DONE = 'done'
    RC_RUNNING = 'running'
    RC_CONNECTED = 'connected'
    RC_ERROR = 'error'
    RC_EXIT = 'exit'

    def __init__(self, verbose=False):
        super(GDBMIResultHandler, self).__init__(None, verbose)
        self.result_class = None
        self.result_str = None

    def _parse_rc(self, ln, rc):
        rc_str = "{0}{1}".format(self.TAG, rc)
        if ln.startswith(rc_str):
            self.result_class = rc
            sl = len(rc_str)
            if len(ln) > sl:
                self.result_str = ln[sl:]
                if self.result_str.startswith(','):
                    self.result_str = self.result_str[1:]
                else:
                    print "Invalid result format: '%s'" % ln
            else:
                self.result_str = ''
            return True
        return False

    def execute(self, ln):
        GDBMIOutRecordHandler.execute(self, ln)
        if self._parse_rc(ln, self.RC_DONE):
            return
        if self._parse_rc(ln, self.RC_RUNNING):
            return
        if self._parse_rc(ln, self.RC_CONNECTED):
            return
        if self._parse_rc(ln, self.RC_ERROR):
            return
        if self._parse_rc(ln, self.RC_EXIT):
            return
        print "Unknown result: '%s'" % ln


class GDBMIStreamConsoleHandler(GDBMIOutStreamHandler):
    """ TBD
    """
    TAG = '~'


def dbg_corefile(args):
    """ TBD
    """
    print "dbg_corefile %s %s %s" % (args.gdb, args.prog, args.core)
    loader = None
    if not args.core:
        loader = ESPCoreDumpLoader(args.off, port=args.port)
        core_fname = loader.get_corefile_from_flash()
        loader.fgdbcore.close()
#         core_fname = 'esp_core.elf'
    else:
        core_fname = args.core
#     print core_fname
#     return

    p = subprocess.Popen(
            bufsize = 0,
            args = [args.gdb,
                '--nw', # ignore .gdbinit
                '--core=%s' % core_fname, # core file
                args.prog],
            stdin = None, stdout = None, stderr = None,
            close_fds = True
            )
    p.wait()
    if loader:
        loader.remove_tmp_file(loader.fgdbcore.name)
        loader.cleanup()
    print 'Done!'


def info_corefile(args):
# def info_corefile(args):
    """ TBD
    """
    print "info_corefile %s %s %s" % (args.gdb, args.prog, args.core)


    def gdbmi_console_stream_handler(ln):
    #     print ln
        sys.stdout.write(ln)
        sys.stdout.flush()
    
    
    def gdbmi_read2prompt(f, out_handlers=None):
        """ TBD
        """
        while True:
            ln = f.readline().rstrip(' \n')
    #         print "LINE='{0}'".format(ln)
            if ln == '(gdb)':
                break
            elif len(ln) == 0:
                break
            elif out_handlers:
                for h in out_handlers:
                    if ln.startswith(out_handlers[h].TAG):
                        out_handlers[h].execute(ln)
                        break

    loader = None
    if not args.core:
        loader = ESPCoreDumpLoader(args.off, port=args.port)
        core_fname = loader.get_corefile_from_flash()
        loader.fgdbcore.close()
    else:
        core_fname = args.core

    handlers = {}
    handlers[GDBMIResultHandler.TAG] = GDBMIResultHandler(verbose=False)
    handlers[GDBMIStreamConsoleHandler.TAG] = GDBMIStreamConsoleHandler(None, verbose=False)
    p = subprocess.Popen(
            bufsize = 0,
            args = [args.gdb,
                '--quiet', # inhibit dumping info at start-up
                '--nx', # inhibit window interface
                '--nw', # ignore .gdbinit
                '--interpreter=mi2', # use GDB/MI v2
                '--core=%s' % core_fname, # core file
                args.prog],
#                 ],
            stdin = subprocess.PIPE, stdout = subprocess.PIPE, stderr = subprocess.STDOUT,
            close_fds = True
            )

    gdbmi_read2prompt(p.stdout, handlers)
    exe_elf = ESPCoreDumpFile(args.prog)
    core_elf = ESPCoreDumpFile(core_fname)
    merged_segs = []#[(s, 0) for s in exe_elf.sections if s.flags & (esptool.ELFSection.SHF_ALLOC | esptool.ELFSection.SHF_WRITE)]
    for s in exe_elf.sections:
        merged = False
        for ps in core_elf.program_segments:
            if ps.addr <= s.addr and ps.addr + len(ps.data) >= s.addr:
                # sec:    |XXXXXXXXXX|
                # seg: |...XXX.............|
                seg_addr = ps.addr
                if ps.addr + len(ps.data) <= s.addr + len(s.data):
                    # sec:        |XXXXXXXXXX|
                    # seg:    |XXXXXXXXXXX...|
                    # merged: |XXXXXXXXXXXXXX|
                    seg_len = len(s.data) + (s.addr - ps.addr)
                else:
                    # sec:        |XXXXXXXXXX|
                    # seg:    |XXXXXXXXXXXXXXXXX|
                    # merged: |XXXXXXXXXXXXXXXXX|
                    seg_len = len(ps.data)
                merged_segs.append((s.name, seg_addr, seg_len, s.attr_str(), True))
                merged = True
            elif ps.addr >= s.addr and ps.addr <= s.addr + len(s.data):
                # sec:  |XXXXXXXXXX|
                # seg:  |...XXX.............|
                seg_addr = s.addr
                if (ps.addr + len(ps.data)) >= (s.addr + len(s.data)):
                    # sec:    |XXXXXXXXXX|
                    # seg:    |..XXXXXXXXXXX|
                    # merged: |XXXXXXXXXXXXX|
                    seg_len = len(s.data) + (ps.addr + len(ps.data)) - (s.addr + len(s.data))
                else:
                    # sec:    |XXXXXXXXXX|
                    # seg:      |XXXXXX|
                    # merged: |XXXXXXXXXX|
                    seg_len = len(s.data)
                merged_segs.append((s.name, seg_addr, seg_len, s.attr_str(), True))
                merged = True

        if not merged:
            merged_segs.append((s.name, s.addr, len(s.data), s.attr_str(), False))
#                 merged_segs.append(('None', ps.addr, len(ps.data), 'None'))

    print "==============================================================="
    print "==================== ESP32 CORE DUMP START ===================="

    handlers[GDBMIResultHandler.TAG].result_class = None
    handlers[GDBMIStreamConsoleHandler.TAG].func = gdbmi_console_stream_handler
    print "\n================== CURRENT THREAD REGISTERS ==================="
    p.stdin.write("-interpreter-exec console \"info registers\"\n")
    gdbmi_read2prompt(p.stdout, handlers)
    if handlers[GDBMIResultHandler.TAG].result_class != GDBMIResultHandler.RC_DONE:
        print "GDB/MI command failed (%s / %s)!" % (handlers[GDBMIResultHandler.TAG].result_class, handlers[GDBMIResultHandler.TAG].result_str)
    print "\n==================== CURRENT THREAD STACK ====================="
    p.stdin.write("-interpreter-exec console \"bt\"\n")
    gdbmi_read2prompt(p.stdout, handlers)
    if handlers[GDBMIResultHandler.TAG].result_class != GDBMIResultHandler.RC_DONE:
        print "GDB/MI command failed (%s / %s)!" % (handlers[GDBMIResultHandler.TAG].result_class, handlers[GDBMIResultHandler.TAG].result_str)
    print "\n======================== THREADS INFO ========================="
    p.stdin.write("-interpreter-exec console \"info threads\"\n")
    gdbmi_read2prompt(p.stdout, handlers)
    if handlers[GDBMIResultHandler.TAG].result_class != GDBMIResultHandler.RC_DONE:
        print "GDB/MI command failed (%s / %s)!" % (handlers[GDBMIResultHandler.TAG].result_class, handlers[GDBMIResultHandler.TAG].result_str)
    print "\n======================= MEMORY REGIONS ========================"
    print "Name   Address   Size   Attrs"
    for ms in merged_segs:
        print "%s 0x%x 0x%x %s" % (ms[0], ms[1], ms[2], ms[3])
    if args.print_mem:
        print "\n====================== MEMORY CONTENTS ========================"
        for ms in merged_segs:
#             if ms[3].find('W') == -1:
            if not ms[4]:
                continue
            print "%s 0x%x 0x%x %s" % (ms[0], ms[1], ms[2], ms[3])
            p.stdin.write("-interpreter-exec console \"x/%dx 0x%x\"\n" % (ms[2]/4, ms[1]))
            gdbmi_read2prompt(p.stdout, handlers)
            if handlers[GDBMIResultHandler.TAG].result_class != GDBMIResultHandler.RC_DONE:
                print "GDB/MI command failed (%s / %s)!" % (handlers[GDBMIResultHandler.TAG].result_class, handlers[GDBMIResultHandler.TAG].result_str)

    print "\n===================== ESP32 CORE DUMP END ====================="
    print "==============================================================="

    p.terminate()
    p.stdin.close()
    p.stdout.close()
    if loader:
        loader.remove_tmp_file(loader.fgdbcore.name)
        loader.cleanup()
        

def main():
    parser = argparse.ArgumentParser(description='coredumper.py v%s - ESP32 Core Dump Utility' % __version__, prog='coredumper')

    parser.add_argument('--chip', '-c',
                        help='Target chip type',
                        choices=['auto', 'esp32'],
                        default=os.environ.get('ESPTOOL_CHIP', 'auto'))

    parser.add_argument(
        '--port', '-p',
        help='Serial port device',
        default=os.environ.get('ESPTOOL_PORT', esptool.ESPLoader.DEFAULT_PORT))

    parser.add_argument(
        '--baud', '-b',
        help='Serial port baud rate used when flashing/reading',
        type=int,
        default=os.environ.get('ESPTOOL_BAUD', esptool.ESPLoader.ESP_ROM_BAUD))

#     parser.add_argument(
#         '--no-stub',
#         help="Disable launching the flasher stub, only talk to ROM bootloader. Some features will not be available.",
#         action='store_true')

    subparsers = parser.add_subparsers(
        dest='operation',
        help='Run coredumper {command} -h for additional help')

    parser_debug_coredump = subparsers.add_parser(
        'dbg_corefile',
        help='Starts GDB debugging session with specified corefile')
    parser_debug_coredump.add_argument('--gdb', '-g', help='Path to gdb', default='xtensa-esp32-elf-gdb')
    parser_debug_coredump.add_argument('--core', '-c', help='Path to core dump file (if skipped core dump will be read from flash)', type=str)
    parser_debug_coredump.add_argument('--off', '-o', help='Ofsset of coredump partition in flash (type "make partition_table" to see).', type=int, default=0x110000)
    parser_debug_coredump.add_argument('prog', help='Path to program\'s ELF binary', type=str)

    parser_info_coredump = subparsers.add_parser(
        'info_corefile',
        help='Print core dump info from file')
    parser_info_coredump.add_argument('--gdb', '-g', help='Path to gdb', default='xtensa-esp32-elf-gdb')
    parser_info_coredump.add_argument('--print-mem', '-m', help='Print memory dump', action='store_true')
    parser_info_coredump.add_argument('--core', '-c', help='Path to core dump file (if skipped core dump will be read from flash)', type=str)
    parser_info_coredump.add_argument('--off', '-o', help='Ofsset of coredump partition in flash (type "make partition_table" to see).', type=int, default=0x110000)
    parser_info_coredump.add_argument('prog', help='Path to program\'s ELF binary', type=str)

    # internal sanity check - every operation matches a module function of the same name
    for operation in subparsers.choices.keys():
        assert operation in globals(), "%s should be a module function" % operation

    args = parser.parse_args()

    print 'coredumper.py v%s' % __version__

    # operation function can take 1 arg (args), 2 args (esp, arg)
    # or be a member function of the ESPLoader class.

    operation_func = globals()[args.operation]
    operation_func(args)


if __name__ == '__main__':
    try:
        main()
    except ESPCoreDumpError as e:
        print '\nA fatal error occurred: %s' % e
        sys.exit(2)
