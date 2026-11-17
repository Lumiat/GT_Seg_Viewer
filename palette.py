"""Palette and adjacency-aware colouring retained from em_seg_viewer."""
import colorsys
import hashlib
import numpy as np

PALETTE = np.array([
    [round(c * 255) for c in colorsys.hls_to_rgb((i * .61803398875) % 1,
                                               .48 if i % 2 else .64, .73)]
    for i in range(48)
], dtype=np.uint8)


def lab(rgb):
    """OKLab for choosing visually separated neighbouring display colours."""
    c = np.asarray(rgb, dtype=float) / 255
    c = np.where(c <= .04045, c / 12.92, ((c + .055) / 1.055) ** 2.4)
    lms = c @ np.array([[.4122214708,.2119034982,.0883024619],
                        [.5363325363,.6806995451,.2817188376],
                        [.0514459929,.1073969566,.6299787005]])
    return np.cbrt(lms) @ np.array([[.2104542553,1.9779984951,.0259040371],
                                  [.7936177850,-2.4285922050,.7827717662],
                                  [-.0040720468,.4505937099,-.8086757660]])


LAB = lab(PALETTE)


def colour_table(slots, ids, assigned, gaps=False):
    neighbours = [set() for _ in ids]
    for distance in ((1, 3) if gaps else (1,)):
        for a, b in ((slots[:, :-distance], slots[:, distance:]),
                     (slots[:-distance, :], slots[distance:, :])):
            valid = (a != b) & (a != 0) & (b != 0)
            lo = np.minimum(a[valid], b[valid]).astype(np.uint64)
            hi = np.maximum(a[valid], b[valid]).astype(np.uint64)
            for pair in np.unique((lo << np.uint64(32)) | hi):
                first, second = int(pair) >> 32, int(pair) & 0xffffffff
                neighbours[first].add(second)
                neighbours[second].add(first)
    # Keep existing assignments stable while visiting new slices.
    order = sorted(range(1, len(ids)), key=lambda i: (-len(neighbours[i]), int(ids[i])))
    for index in order:
        label = int(ids[index])
        if label in assigned:
            continue
        used = [assigned[int(ids[n])] for n in neighbours[index] if int(ids[n]) in assigned]
        seed = int.from_bytes(hashlib.blake2b(str(label).encode(), digest_size=2).digest(), 'little')
        if used:
            score = ((LAB[:, None, :] - LAB[used][None, :, :]) ** 2).sum(axis=2).min(axis=1)
            chosen = int(np.argmax(score + np.roll(np.arange(len(PALETTE)), seed % len(PALETTE)) * 1e-10))
        else:
            chosen = seed % len(PALETTE)
        assigned[label] = chosen
    return [[0, 0, 0]] + [PALETTE[assigned[int(i)]].tolist() for i in ids[1:]]

