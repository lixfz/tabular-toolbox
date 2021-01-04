# -*- coding:utf-8 -*-
"""

"""
from collections import defaultdict
from functools import partial

import dask.array as da
import dask.dataframe as dd
import numpy as np
import pandas as pd
from dask_ml import model_selection as dm_sel, preprocessing as dm_pre, decomposition as dm_dec
from sklearn import inspection as sk_inspect, metrics as sk_metrics
from sklearn import model_selection as sk_sel, preprocessing as sk_pre, utils as sk_utils
from sklearn.base import BaseEstimator, TransformerMixin

from .utils import logging

logger = logging.get_logger(__name__)


def is_dask_dataframe(X):
    return isinstance(X, dd.DataFrame)


def is_dask_series(X):
    return isinstance(X, dd.Series)


def is_dask_dataframe_or_series(X):
    return isinstance(X, (dd.DataFrame, dd.Series))


def is_dask_array(X):
    return isinstance(X, da.Array)


def is_dask_object(X):
    return isinstance(X, (da.Array, dd.DataFrame, dd.Series))


def exist_dask_object(*args):
    for a in args:
        if isinstance(a, (da.Array, dd.DataFrame, dd.Series)):
            return True
    return False


def exist_dask_dataframe(*args):
    for a in args:
        if isinstance(a, dd.DataFrame):
            return True
    return False


def exist_dask_array(*args):
    for a in args:
        if isinstance(a, da.Array):
            return True
    return False


def to_dask_type(X):
    if isinstance(X, np.ndarray):
        X = da.from_array(X)
    elif isinstance(X, (pd.DataFrame, pd.Series)):
        X = dd.from_pandas(X, npartitions=1)

    return X


def make_chunk_size_known(a):
    assert is_dask_array(a)

    chunks = a.chunks
    if any(np.nan in d for d in chunks):
        if logger.is_debug_enabled():
            logger.debug(f'call extracted array compute_chunk_sizes, shape: {a.shape}')
        a = a.compute_chunk_sizes()
    return a


def make_divisions_known(X):
    assert is_dask_object(X)

    if is_dask_dataframe(X):
        if not X.known_divisions:
            columns = X.columns.tolist()
            X = X.reset_index()
            new_columns = X.columns.tolist()
            index_name = set(new_columns) - set(columns)
            X = X.set_index(list(index_name)[0] if index_name else 'index')
            assert X.known_divisions
    elif is_dask_series(X):
        if not X.known_divisions:
            X = make_divisions_known(X.to_frame())[X.name]
    else:  # dask array
        X = make_chunk_size_known(X)

    return X


def hstack_array(arrs):
    if len(arrs) > 1:
        arrs = [make_chunk_size_known(a) for a in arrs]
    return da.hstack(arrs)


def vstack_array(arrs):
    if len(arrs) > 1:
        arrs = [make_chunk_size_known(a) for a in arrs]
    return da.vstack(arrs)


def concat_df(dfs, axis=0, repartition=False, **kwargs):
    if exist_dask_dataframe(*dfs):
        if axis == 1:
            dfs = [make_divisions_known(df) for df in dfs]
        df = dd.concat(dfs, axis=axis, **kwargs)
        if repartition:
            df = df.repartition(npartitions=dfs[0].npartitions)
    else:
        df = pd.concat(dfs, axis=axis, **kwargs)

    return df


def train_test_split(*data, shuffle=True, random_state=9527, **kwargs):
    if exist_dask_dataframe(*data):
        if len(data) > 1:
            data = [make_divisions_known(to_dask_type(x)) for x in data]
        return dm_sel.train_test_split(*data, shuffle=shuffle, random_state=random_state, **kwargs)
    else:
        return sk_sel.train_test_split(*data, shuffle=shuffle, random_state=random_state, **kwargs)


def permutation_importance(estimator, X, y, *args, scoring=None, n_repeats=5,
                           n_jobs=None, random_state=None):
    if not is_dask_dataframe(X):
        return sk_inspect.permutation_importance(estimator, X, y, *args,
                                                 scoring=scoring,
                                                 n_repeats=n_repeats,
                                                 n_jobs=n_jobs,
                                                 random_state=random_state)
    random_state = sk_utils.check_random_state(random_state)

    def wrap_estimator(est):
        def call_and_compute(fn, *args, **kwargs):
            r = fn(*args, **kwargs)
            if is_dask_object(r):
                r = r.compute()
            return r

        if hasattr(est, 'predict_proba'):
            orig_predict_proba = est.predict_proba
            setattr(est, '_orig_predict_proba', orig_predict_proba)
            setattr(est, 'predict_proba', partial(call_and_compute, orig_predict_proba))

        if hasattr(est, 'predict'):
            orig_predict = est.predict
            setattr(est, '_orig_predict', orig_predict)
            setattr(est, 'predict', partial(call_and_compute, orig_predict))

        return est

    def shuffle_partition(df, col_idx):
        shuffling_idx = np.arange(df.shape[0])
        random_state.shuffle(shuffling_idx)
        col = df.iloc[shuffling_idx, col_idx]
        col.index = df.index
        df.iloc[:, col_idx] = col
        return df

    if is_dask_object(y):
        y = y.compute()

    scorer = sk_metrics.check_scoring(wrap_estimator(estimator), scoring)
    baseline_score = scorer(estimator, X, y)
    scores = []

    for c in range(X.shape[1]):
        col_scores = []
        for i in range(n_repeats):
            X_permuted = X.copy().map_partitions(shuffle_partition, c)
            col_scores.append(scorer(estimator, X_permuted, y))
        if logger.is_debug_enabled():
            logger.debug(f'permuted scores [{X.columns[c]}]: {col_scores}')
        scores.append(col_scores)

    importances = baseline_score - np.array(scores)
    return sk_utils.Bunch(importances_mean=np.mean(importances, axis=1),
                          importances_std=np.std(importances, axis=1),
                          importances=importances)


class MultiLabelEncoder(BaseEstimator, TransformerMixin):
    def __init__(self):
        self.encoders = {}

    def fit(self, X, y=None):
        assert len(X.shape) == 2

        if isinstance(X, (pd.DataFrame, dd.DataFrame)):
            return self._fit_df(X, y)
        elif isinstance(X, (np.ndarray, da.Array)):
            return self._fit_array(X, y)
        else:
            raise Exception(f'Unsupported type "{type(X)}"')

    def _fit_df(self, X, y=None):
        return self._fit_array(X.values, y.values if y else None)

    def _fit_array(self, X, y=None):
        n_features = X.shape[1]
        for n in range(n_features):
            le = dm_pre.LabelEncoder()
            le.fit(X[:, n])
            self.encoders[n] = le
        return self

    def transform(self, X):
        assert len(X.shape) == 2

        if isinstance(X, (dd.DataFrame, pd.DataFrame)):
            return self._transform_dask_df(X)
        elif isinstance(X, (da.Array, np.ndarray)):
            return self._transform_dask_array(X)
        else:
            raise Exception(f'Unsupported type "{type(X)}"')

    def _transform_dask_df(self, X):
        data = self._transform_dask_array(X.values)

        if isinstance(X, dd.DataFrame):
            result = dd.from_dask_array(data, columns=X.columns)
        else:
            result = pd.DataFrame(data, columns=X.columns)
        return result

    def _transform_dask_array(self, X):
        n_features = X.shape[1]
        assert n_features == len(self.encoders.items())

        data = []
        for n in range(n_features):
            data.append(self.encoders[n].transform(X[:, n]))

        if isinstance(X, da.Array):
            result = da.stack(data, axis=-1, allow_unknown_chunksizes=True)
        else:
            result = np.stack(data, axis=-1)

        return result

    # def fit_transform(self, X, y=None):
    #     return self.fit(X, y).transform(X)


class OneHotEncoder(dm_pre.OneHotEncoder):
    def fit(self, X, y=None):
        if isinstance(X, (dd.DataFrame, pd.DataFrame)) and self.categories == "auto" \
                and any(d.name == 'object' for d in X.dtypes):
            a = []
            if isinstance(X, dd.DataFrame):
                for i in range(len(X.columns)):
                    Xi = X.iloc[:, i]
                    if Xi.dtype == 'object':
                        Xi = Xi.astype('category').cat.as_known()
                    a.append(Xi)
                X = dd.concat(a, axis=1, ignore_unknown_divisions=True)
            else:
                for i in range(len(X.columns)):
                    Xi = X.iloc[:, i]
                    if Xi.dtype == 'object':
                        Xi = Xi.astype('category')
                    a.append(Xi)
                X = pd.concat(a, axis=1)

        return super(OneHotEncoder, self).fit(X, y)

    def get_feature_names(self, input_features=None):
        if not hasattr(self, 'drop_idx_'):
            setattr(self, 'drop_idx_', None)
        return super(OneHotEncoder, self).get_feature_names(input_features)


class TruncatedSVD(dm_dec.TruncatedSVD):
    def fit_transform(self, X, y=None):
        X_orignal = X
        if isinstance(X, pd.DataFrame):
            X = dd.from_pandas(X, npartitions=2)

        if isinstance(X, dd.DataFrame):
            # y = y.values.compute_chunk_sizes() if y is not None else None
            r = super(TruncatedSVD, self).fit_transform(X.values.compute_chunk_sizes(), None)
        else:
            r = super(TruncatedSVD, self).fit_transform(X, y)

        if isinstance(X_orignal, (pd.DataFrame, np.ndarray)):
            r = r.compute()
        return r  # fixme, restore to DataFrame ??

    def transform(self, X, y=None):
        if isinstance(X, dd.DataFrame):
            return super(TruncatedSVD, self).transform(X.values, y)

        return super(TruncatedSVD, self).transform(X, y)

    def inverse_transform(self, X):
        if isinstance(X, dd.DataFrame):
            return super(TruncatedSVD, self).inverse_transform(X.values)

        return super(TruncatedSVD, self).inverse_transform(X)


class MaxAbsScaler(sk_pre.MaxAbsScaler):
    __doc__ = sk_pre.MaxAbsScaler.__doc__

    def fit(self, X, y=None, ):
        from dask_ml.utils import handle_zeros_in_scale

        self._reset()
        if isinstance(X, (pd.DataFrame, np.ndarray)):
            return super().fit(X, y)

        max_abs = X.reduction(lambda x: x.abs().max(),
                              aggregate=lambda x: x.max(),
                              token=self.__class__.__name__
                              ).compute()
        scale = handle_zeros_in_scale(max_abs)

        setattr(self, 'max_abs_', max_abs)
        setattr(self, 'scale_', scale)
        setattr(self, 'n_samples_seen_', 0)

        self.n_features_in_ = X.shape[1]
        return self

    def partial_fit(self, X, y=None, ):
        raise NotImplementedError()

    def transform(self, X, y=None, copy=None, ):
        if isinstance(X, (pd.DataFrame, np.ndarray)):
            return super().transform(X)

        # Workaround for https://github.com/dask/dask/issues/2840
        if isinstance(X, dd.DataFrame):
            X = X.div(self.scale_)
        else:
            X = X / self.scale_
        return X

    def inverse_transform(self, X, y=None, copy=None, ):
        if not hasattr(self, "scale_"):
            raise Exception(
                "This %(name)s instance is not fitted yet. "
                "Call 'fit' with appropriate arguments before "
                "using this method."
            )

        if isinstance(X, (pd.DataFrame, np.ndarray)):
            return super().inverse_transform(X)

        if copy:
            X = X.copy()
        if isinstance(X, dd.DataFrame):
            X = X.mul(self.scale_)
        else:
            X = X * self.scale_

        return X


class SafeOrdinalEncoder(BaseEstimator, TransformerMixin):
    __doc__ = r'Adapted from dask_ml OrdinalEncoder\n' + dm_pre.OrdinalEncoder.__doc__

    def __init__(self, columns=None, dtype=np.float64):
        self.columns = columns
        self.dtype = dtype

    def fit(self, X, y=None):
        """Determine the categorical columns to be encoded.

        Parameters
        ----------
        X : pandas.DataFrame or dask.dataframe.DataFrame
        y : ignored

        Returns
        -------
        self
        """
        self.columns_ = X.columns
        self.dtypes_ = {c: X[c].dtype for c in X.columns}

        if self.columns is None:
            columns = X.select_dtypes(include=["category", 'object']).columns
        else:
            columns = self.columns

        X = X.categorize(columns=columns)

        self.categorical_columns_ = columns
        self.non_categorical_columns_ = X.columns.drop(self.categorical_columns_)
        self.categories_ = {c: X[c].cat.categories.sort_values() for c in columns}

        return self

    def transform(self, X, y=None):
        """Ordinal encode the categorical columns in X

        Parameters
        ----------
        X : pd.DataFrame or dd.DataFrame
        y : ignored

        Returns
        -------
        transformed : pd.DataFrame or dd.DataFrame
            Same type as the input
        """
        if not X.columns.equals(self.columns_):
            raise ValueError(
                "Columns of 'X' do not match the training "
                "columns. Got {!r}, expected {!r}".format(X.columns, self.columns)
            )

        encoder = self.make_encoder(self.categorical_columns_, self.categories_, self.dtype)
        if isinstance(X, pd.DataFrame):
            X = encoder(X)
        elif isinstance(X, dd.DataFrame):
            X = X.map_partitions(encoder)
        else:
            raise TypeError("Unexpected type {}".format(type(X)))

        return X

    def inverse_transform(self, X, missing_value=None):
        """Inverse ordinal-encode the columns in `X`

        Parameters
        ----------
        X : array or dataframe
            Either the NumPy, dask, or pandas version

        missing_value : skip doc

        Returns
        -------
        data : DataFrame
            Dask array or dataframe will return a Dask DataFrame.
            Numpy array or pandas dataframe will return a pandas DataFrame
        """
        if isinstance(X, np.ndarray):
            X = pd.DataFrame(X, columns=self.columns_)
        elif isinstance(X, da.Array):
            # later on we concat(..., axis=1), which requires
            # known divisions. Suboptimal, but I think unavoidable.
            unknown = np.isnan(X.chunks[0]).any()
            if unknown:
                lengths = da.blockwise(len, "i", X[:, 0], "i", dtype="i8").compute()
                X = X.copy()
                chunks = (tuple(lengths), X.chunks[1])
                X._chunks = chunks
            X = dd.from_dask_array(X, columns=self.columns_)

        decoder = self.make_decoder(self.categorical_columns_, self.categories_, self.dtypes_)

        if isinstance(X, dd.DataFrame):
            X = X.map_partitions(decoder)
        else:
            X = decoder(X)

        return X

    @staticmethod
    def make_encoder(columns, categories, dtype):
        mappings = {}
        for col in columns:
            cat = categories[col]
            unseen = len(cat)
            m = defaultdict(dtype)
            for k, v in zip(cat, range(unseen)):
                m[k] = dtype(v + 1)
            mappings[col] = m

        def encode_column(x, c):
            return mappings[c][x]

        def safe_ordinal_encoder(pdf):
            assert isinstance(pdf, pd.DataFrame)

            pdf = pdf.copy()
            vf = np.vectorize(encode_column, excluded='c', otypes=[dtype])
            for col in columns:
                r = vf(pdf[col].values, col)
                if r.dtype != dtype:
                    # print(r.dtype, 'astype', dtype)
                    r = r.astype(dtype)
                pdf[col] = r
            return pdf

        return safe_ordinal_encoder

    @staticmethod
    def make_decoder(columns, categories, dtypes):
        def decode_column(x, col):
            cat = categories[col]
            xi = int(x)
            unseen = cat.shape[0]  # len(cat)
            if unseen >= xi >= 1:
                return cat[xi - 1]
            else:
                dtype = dtypes[col]
                if dtype in (np.float32, np.float64, np.float):
                    return np.nan
                elif dtype in (np.int32, np.int64, np.int, np.uint32, np.uint64, np.uint):
                    return -1
                else:
                    return None

        def safe_ordinal_decoder(pdf):
            assert isinstance(pdf, pd.DataFrame)

            pdf = pdf.copy()
            for col in columns:
                vf = np.vectorize(decode_column, excluded='col', otypes=[dtypes[col]])
                pdf[col] = vf(pdf[col].values, col)
            return pdf

        return safe_ordinal_decoder


class DataInterceptEncoder(BaseEstimator, TransformerMixin):

    def __init__(self, fit=False, fit_transform=False, transform=False, inverse_transform=False):
        self._intercept_fit = fit
        self._intercept_fit_transform = fit_transform
        self._intercept_transform = transform
        self._intercept_inverse_transform = inverse_transform

        super(DataInterceptEncoder, self).__init__()

    def fit(self, X, *args, **kwargs):
        if self._intercept_fit:
            self.intercept(X, *args, **kwargs)

        return self

    def fit_transform(self, X, *args, **kwargs):
        if self._intercept_fit_transform:
            X = self.intercept(X, *args, **kwargs)

        return X

    def transform(self, X, *args, **kwargs):
        if self._intercept_transform:
            X = self.intercept(X, *args, **kwargs)

        return X

    def inverse_transform(self, X, *args, **kwargs):
        if self._intercept_inverse_transform:
            X = self.intercept(X, *args, **kwargs)

        return X

    def intercept(self, X, *args, **kwargs):
        raise NotImplementedError()


class CallableAdapterEncoder(DataInterceptEncoder):
    def __init__(self, fn, **kwargs):
        assert callable(fn)

        self.fn = fn

        super(CallableAdapterEncoder, self).__init__(**kwargs)

    def intercept(self, X, *args, **kwargs):
        return self.fn(X, *args, **kwargs)


class DataCacher(DataInterceptEncoder):
    """
    persist and cache dask dataframe and array
    """

    def __init__(self, cache_dict, cache_key, remove_keys=None, **kwargs):
        assert isinstance(cache_dict, dict)

        if isinstance(remove_keys, str):
            remove_keys = set(remove_keys.split(','))

        self._cache_dict = cache_dict
        self.cache_key = cache_key
        self.remove_keys = remove_keys

        super(DataCacher, self).__init__(**kwargs)

    def intercept(self, X, *args, **kwargs):
        if self.cache_key:
            if isinstance(X, (dd.DataFrame, da.Array)):
                if logger.is_debug_enabled():
                    logger.debug(f'persist and cache {X._name} as {self.cache_key}')

                X = X.persist()

            self._cache_dict[self.cache_key] = X

        if self.remove_keys:
            for key in self.remove_keys:
                if key in self._cache_dict.keys():
                    if logger.is_debug_enabled():
                        logger.debug(f'remove cache {key}')
                    del self._cache_dict[key]

        return X

    @property
    def cache_dict(self):
        return list(self._cache_dict.keys())


class CacheCleaner(DataInterceptEncoder):

    def __init__(self, cache_dict, **kwargs):
        assert isinstance(cache_dict, dict)

        self._cache_dict = cache_dict

        super(CacheCleaner, self).__init__(**kwargs)

    def intercept(self, X, *args, **kwargs):
        if logger.is_debug_enabled():
            logger.debug(f'clean cache with {list(self._cache_dict.keys())}')
        self._cache_dict.clear()

        return X

    @property
    def cache_dict(self):
        return list(self._cache_dict.keys())

    # # override this to remove 'cache_dict' from estimator __expr__
    # @classmethod
    # def _get_param_names(cls):
    #     params = super()._get_param_names()
    #     return [p for p in params if p != 'cache_dict']
