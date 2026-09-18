from lib.identity import elf, pe
from lib.refusal import Refusal

FORMATS = ((b"\x7fELF", elf.locate), (b"MZ", pe.locate))


def locate(image):
    for magic, locator in FORMATS:
        if image[:len(magic)] == magic:
            return locator(image)
    raise Refusal("identity binding locates ELF and PE executables only")
