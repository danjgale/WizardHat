"""

"""

import wizardhat.utils as utils

import datetime
import json
import os
import threading

import numpy as np


class Data:
    """Abstract base class of data management classes.

    Provides management of instance-related filenames and pipeline metadata for
    subclasses. Pipeline metadata consists of a field in the `metadata`
    attribute which tracks the `Data` and `transform.Transformer` subclasses
    through which the data has flowed. Complete instance metadata is written
    to a `.json` file with the same name as the instance's data file (minus
    its extension). Therefore, each data file corresponds to a `.json` file
    that describes how the data was generated.

    As an abstract class, `Data` should not be instantiated directly, but must
    be subclassed (e.g. `TimeSeries`). Subclasses should conform to expected
    behaviour by overriding methods or properties that raise
    `NotImplementedError`. A general implementation of the `data` property is
    provided, which will raise a `NotImplementedError` if the `_data` attribute
    is undefined; thus, instance data should be stored internally in
    `self._data`.

    Does not operate in a separate thread, but is accessed by separate threads.
    Subclasses should use the thread lock so that IO operations from multiple
    threads do not violate the data (e.g. adding a new row midway through
    returning a copy).

    Also provides an implementation of `__deepcopy__`, so that an independent
    but otherwise identical instance can be cloned from an existing instance
    using `copy.deepcopy`. This may be useful for `transform.Transformer`
    subclasses that want to output data in a similar form to their input.

    Args:
        metadata (dict): Arbitrary information added to instance's `.json`.
        filename (str): User-defined filename for saving instance (meta)data.
            By default, a name is generated based on the date, the class name
            of the instance, the user-defined label (if specified), and an
            incrementing integer to prevent overwrites. For example,
            "2018-03-01_TimeSeries_somelabel_0".
        data_dir (str): Directory for saving instance (meta)data.
            May be relative (e.g. "data" or "./data") or absolute.
            Defaults to "./data".
        label (str): User-defined addition to standard filename.

    Attributes:
        updated (threading.Event): Flag for threads waiting for data updates.
        filename (str): Final (generated or specified) filename for writing.
        metadata (dict): All metadata included in instance's `.json`.

    Todo:
        * Implement with abc.ABC (prevent instantiation of Data itself)
        * Detailed pipeline metadata: not only class names but attribute values
        * Decorator for locked methods
    """

    def __init__(self, metadata=None, filename=None, data_dir='./data',
                 label=''):

        # thread control
        self._lock = threading.Lock()
        self.updated = threading.Event()

        # file output preparations
        if not data_dir[0] in ['.', '/']:
            data_dir = './' + data_dir
        if filename is None:
            filename = self._new_filename(data_dir, label)
        utils.makedirs(filename)
        self.filename = filename
        self._data_dir = data_dir
        self._label = label

        # metadata
        if metadata is None:
            metadata = {}
        try:
            # initialize if necessary
            metadata.setdefault('pipeline', [])
        except TypeError:
            raise TypeError("Metadata must be a dict")
        self.metadata = metadata
        # add subclass information to pipeline metadata and write to file
        self.update_pipeline_metadata(self)

    @property
    def data(self):
        """A complete copy of instance data.

        Copying prevents unwanted modification due to passing-by-reference.

        A general implementation is provided,
        """
        try:
            with self._lock:
                return np.copy(self._data)
        except AttributeError:
            raise NotImplementedError()

    def initialize(self):
        """Reset instance data; e.g. to zeros.

        May also contain other expressions necessary for initialization; for
        example, resetting the count of samples received.
        """
        raise NotImplementedError()

    def update(self):
        """Update instance data; e.g. by appending rows of new data."""
        raise NotImplementedError()

    def update_pipeline_metadata(self, obj):
        """Add some object's details to the instance's pipeline metadata.

        Automatically updates the instance's metadata `.json` file with the new
        information.

        Args:
            obj (object): The object to be represented in metadata.
        """
        self.metadata['pipeline'].append(type(obj).__name__)
        self._write_metadata_to_file()

    def _write_metadata_to_file(self):
        try:
            metadata_json = json.dumps(self.metadata, indent=4)
        except TypeError:
            raise TypeError("JSON could not serialize metadata")
        with open(self.filename + '.json', 'w') as f:
            f.write(metadata_json)

    def _new_filename(self, data_dir='data', label=''):
        date = datetime.date.today().isoformat()
        classname = type(self).__name__
        if label:
            label += '_'

        filename = '{}/{}_{}_{}{{}}'.format(data_dir, date, classname, label)
        # incremental counter to prevent overwrites
        # (based on existence of metadata file)
        count = 0
        while os.path.exists(filename.format(count) + '.json'):
            count += 1
        filename = filename.format(count)

        return filename

    def __deepcopy__(self, memo):
        # threading objects cannot be copied normally
        # & a new filename is needed
        mask = {'_lock': threading.Lock(),
                'updated': threading.Event(),
                'filename': self._new_filename(self._data_dir, self._label)}
        return utils.deepcopy_mask(self, mask, memo)


class TimeSeries(Data):
    """Manages 2D time series data: rows of samples indexed in order of time.

    Data is stored in a NumPy structured array where `'time'` is the first
    field (named column) and the remaining fields correspond to the channel
    names passed during instantiation. Only the last `n_samples` are stored
    in memory this way, but all samples are written to disk when `record=True`.

    Attributes:
        dtype (np.dtype): The data type of the data's NumPy structured array.

    TODO:
        * Warning (error?) when timestamps are out of order
        * Record to disk on stopping
    """

    def __init__(self, ch_names, n_samples=2560, record=True, channel_fmt='f8',
                 **kwargs):
        """Create a new `TimeSeries` object.

        Args:
            ch_names (List[str]): List of channel names.
            n_samples (int): Number of samples to store in memory.
            record (bool): Whether to record samples to disk.
            channel_fmt (str or type or List[str] or List[type]): Data type of
                channels. If a single string or type is passed, all channels
                will take that type. A list with the same length as `ch_names`
                may also be passed to independently specify channel types.
                Strings should conform to NumPy string datatype specifications;
                for example, a 64-bit float is specified as `'f8'`. Types may
                be Python base types (e.g. `float`) or NumPy base dtypes
               ( e.g. `np.float64`).
        """
        Data.__init__(self, **kwargs)

        if str(channel_fmt) == channel_fmt:  # quack
            channel_fmt = [channel_fmt] * len(ch_names)
        try:
            self.dtype = np.dtype({'names': ["time"] + ch_names,
                                   'formats': [np.float64] + channel_fmt})
        except ValueError:
            raise ValueError("Number of formats must match number of channels")

        self.initialize(n_samples)

    @classmethod
    def with_window(cls, ch_names, sfreq, window=10, **kwargs):
        """Constructs an instance based on a desired duration of storage.

        It is often convenient to specify the length of the array stored in
        memory as a duration. For example, a duration of 10 seconds might be
        specified so that the last 10 seconds of data will be available to an
        instance of `plot.Plotter`.

        This constructor also expects to be passed a nominal sampling frequency
        so that it can determine the number of samples corresponding to the
        desired duration. Note that duration is usually not evenly divisible by
        sampling frequency, so that the number of samples stored

        Args:
            ch_names (List[str]): List of channel names.
            sfreq (int): Nominal sampling frequency of the input.
            window (float): Desired duration of live storage.
        """
        n_samples = int(window * sfreq)
        return cls(ch_names, n_samples, **kwargs)

    def initialize(self, n_samples):
        """Initialize NumPy structured array for data storage.

        Args:
            n_samples (int): Number of samples (rows) in array.
        """
        with self._lock:
            self._data = np.zeros((self.n_samples,), dtype=self.dtype)
        self._count = self.n_samples

    def update(self, timestamps, samples):
        """Append sample(s) to stored data.

        Args:
            timestamps (Iterable[np.float64]): Timestamp for each sample.
            samples (Iterable): Channel data.
                Data type(s) in `Iterable` correspond to the type(s) specified
                in `dtype`.
        """
        new = self._format_samples(timestamps, samples)

        self._count -= len(new)
        cutoff = len(new) + self._count
        self._append(new[:cutoff])
        if self._count < 1:
            if self.record:
                self._write_to_file()
            self._append(new[cutoff:])
            self._count = self.n_samples

        self.updated.set()

    def _append(self, new):
        with self._lock:
            self._data = utils.push_rows(self._data, new)

    def _write_to_file(self):
        with self._lock:
            with open(self.filename + ".csv", 'a') as f:
                for row in self._data:
                    line = ','.join(str(n) for n in row)
                    f.write(line + '\n')

    def _format_samples(self, timestamps, samples):
        """Format data `numpy.ndarray` from timestamps and samples."""
        stacked = [(t,) + tuple(s) for t, s in zip(timestamps, samples)]
        return np.array(stacked, dtype=self.dtype)

    @property
    def n_samples(self):
        """Number of samples stored in the NumPy array."""
        return self._data.shape[0]

    @property
    def ch_names(self):
        """Channel names.

        Note:
            Does not include `'time'`.
        """
        # Assumes 'time' is in first column
        return self.dtype.names[1:]

    @property
    def samples(self):
        """Return copy of channel data, without timestamps."""
        return self.data[self.ch_names]

    @property
    def last(self):
        """Last-stored row (timestamp and sample)."""
        with self._lock:
            return np.copy(self._data[-1])
