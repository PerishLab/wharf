from lib.identity import elf, macho, pe
from lib.refusal import Refusal

FORMATS = ((b"\x7fELF", elf.locate), (b"MZ", pe.locate), (macho.MAGIC, macho.locate))


def locate(image):
    for magic, locator in FORMATS:
        if image[:len(magic)] == magic:
            return locator(image)
    raise Refusal("identity binding locates ELF, PE and thin Mach-O executables only")
