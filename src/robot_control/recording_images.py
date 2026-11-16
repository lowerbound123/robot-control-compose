"""RC-owned references to the latest successfully published image per camera."""


class PublishedFrames:
    def __init__(self):
        self._epoch = None
        self._frames = {}

    def clear(self):
        self._frames = {}
        self._epoch = None

    def select(self, published, descriptors, *, epoch):
        if epoch != self._epoch:
            self.clear()
            self._epoch = epoch
        selected, retained = {}, {}
        for name, descriptor in descriptors.items():
            candidate = self._frames.get(name)
            if name in published:
                pixels, timestamp = published[name]
                if (not pixels.flags.writeable and timestamp == descriptor['timestamp']
                        and pixels.nbytes == descriptor['nbytes']
                        and list(pixels.shape) == descriptor['shape']
                        and pixels.dtype.str == descriptor['dtype']):
                    candidate = (dict(descriptor), pixels)
                else:
                    candidate = None
            if candidate is not None and candidate[0] == descriptor:
                retained[name] = candidate
                selected[name] = (candidate[1], descriptor['timestamp'])
        # One reference per currently leased camera, no historic frame cache.
        self._frames = retained
        return selected
