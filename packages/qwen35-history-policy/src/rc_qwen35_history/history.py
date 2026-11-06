"""Owned, immutable frames indexed by successfully consumed action targets."""
from collections import deque
from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class HistoryFrame:
    index: int
    image: np.ndarray


class HistoryBuffer:
    def __init__(self, count, stride, resize):
        if type(count) is not int or count < 1 or type(stride) is not int or stride < 1:
            raise ValueError('history count and stride must be positive integers')
        self.count, self.stride, self.resize = count, stride, resize
        self.frames = deque(maxlen=count * stride + 1)
        self.tick = 0

    def reset(self):
        self.frames.clear()
        self.tick = 0

    def append(self, image):
        # resize may return a view: always detach from the callback's mmap.
        owned = np.array(self.resize(image), dtype=np.uint8, copy=True, order='C')
        owned.flags.writeable = False
        self.frames.append(HistoryFrame(self.tick, owned))
        self.tick += 1

    def snapshot(self):
        first = self.tick - len(self.frames)
        return tuple(self.frames[index - first]
                     for index in range(self.tick - self.count*self.stride, self.tick, self.stride)
                     if index >= max(0, first))
