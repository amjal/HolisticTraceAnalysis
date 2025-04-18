# Copyright (c) Meta Platforms, Inc. and affiliates.
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

from collections import defaultdict
from typing import Dict, List, Optional, Tuple, TYPE_CHECKING

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
from hta.common.trace_filter import GPUKernelFilter
from hta.common.trace_symbol_table import decode_symbol_id_to_symbol_name

from hta.configs.config import logger
from hta.utils.utils import (
    get_kernel_type,
    IdleTimeType,
    KernelType,
    merge_kernel_intervals,
)
from plotly.subplots import make_subplots

# import statement used without the "if TYPE_CHECKING" guard will cause a circular
# dependency with trace_analysis.py causing mypy to fail and should not be removed.
if TYPE_CHECKING:
    from hta.common.trace import Trace

# This configures the threshold under which we consider gaps between
# kernels to be due to realistic delays in launching back-back kernels on the GPU


class BreakdownAnalysis:
    def __init__(self):
        pass

    @classmethod
    def get_gpu_kernel_breakdown(
        cls,
        t: "Trace",
        visualize: bool = True,
        duration_ratio: float = 0.8,
        num_kernels: int = 10,
        include_memory_kernels: bool = False,
        image_renderer="notebook",
    ) -> Tuple[pd.DataFrame, pd.DataFrame]:
        """
        GPU kernel breakdown implementation. See `get_gpu_kernel_breakdown` in `trace_analysis.py` for details.
        """
        sym_table = t.symbol_table.get_sym_table()

        all_kernel_df = pd.DataFrame(
            {
                "name": pd.Series(dtype="str"),
                "sum": pd.Series(dtype="int"),
                "max": pd.Series(dtype="int"),
                "min": pd.Series(dtype="int"),
                "std": pd.Series(dtype="float"),
                "mean": pd.Series(dtype="int"),
                "kernel_type": pd.Series(dtype="str"),
                "rank": pd.Series(dtype="int"),
            }
        )
        kernel_type_df = pd.DataFrame(
            {
                "kernel_type": pd.Series(dtype="str"),
                "sum": pd.Series(dtype="int"),
            }
        )

        kernel_type_to_analysis: List[str] = [
            KernelType.COMPUTATION.name,
            KernelType.COMMUNICATION.name,
        ]
        if include_memory_kernels:
            kernel_type_to_analysis.append(KernelType.MEMORY.name)

        kernel_per_rank: Dict[str, Dict] = defaultdict(dict)
        for rank, trace_df in t.traces.items():
            gpu_kernels = trace_df[trace_df["stream"].ne(-1)].copy()
            gpu_kernels["kernel_type"] = gpu_kernels[["name"]].apply(
                lambda x: get_kernel_type(sym_table[x["name"]]), axis=1
            )
            gpu_kernels["name"] = gpu_kernels["name"].apply(lambda x: sym_table[x])

            # Create kernel type dataframe
            kernel_type_df = pd.concat(
                [
                    kernel_type_df,
                    cls._get_gpu_kernel_type_time(gpu_kernels, kernel_type_to_analysis),
                ],
                ignore_index=True,
            )

            # Create all kernel info dataframe
            for kernel_type in kernel_type_to_analysis:
                gpu_kernel_time = gpu_kernels[gpu_kernels["kernel_type"] == kernel_type]

                if kernel_type not in kernel_per_rank:
                    kernel_per_rank[kernel_type] = {}

                gpu_kernel_time = cls._aggr_gpu_kernel_time(
                    gpu_kernel_time,
                    duration_ratio=duration_ratio,
                    num_kernels=num_kernels,
                )

                kernel_per_rank[kernel_type][rank] = gpu_kernel_time

                gpu_kernel_time["kernel_type"] = kernel_type
                gpu_kernel_time["rank"] = int(rank)
                all_kernel_df = pd.concat(
                    [all_kernel_df, gpu_kernel_time], ignore_index=True
                )

        kernel_type_df = kernel_type_df.groupby(by=["kernel_type"])["sum"].agg(["sum"])
        kernel_type_df.reset_index(inplace=True)
        kernel_type_df.sort_values(
            by=["sum"], ignore_index=True, inplace=True, ascending=False
        )
        kernel_type_df["percentage"] = (
            kernel_type_df["sum"] / kernel_type_df["sum"].sum()
        ) * 100
        kernel_type_df = kernel_type_df.round({"percentage": 1})

        all_kernel_df.sort_values(
            by=["kernel_type", "name", "rank"], ignore_index=True, inplace=True
        )
        all_kernel_df.rename(
            columns={
                "sum": "sum (us)",
                "mean": "mean (us)",
                "max": "max (us)",
                "min": "min (us)",
                "std": "stddev",
            },
            inplace=True,
        )

        if visualize:  # pragma: no cover
            non_zero_kernel_df = kernel_type_df[(kernel_type_df["percentage"] > 0)]

            fig = px.pie(
                non_zero_kernel_df,
                values="percentage",
                names="kernel_type",
                height=500,
                title="Kernel Type Percentage Across All Ranks",
            )
            fig.update_layout(
                margin=dict(l=50, r=50, b=50, t=50),
                showlegend=True,
                legend=dict(yanchor="bottom", y=-0.4, xanchor="left", x=0),
            )
            fig.show(renderer=image_renderer)

            for kernel in kernel_per_rank:
                specs = []
                for count, rank in enumerate(kernel_per_rank[kernel]):
                    if count % 2 == 0:
                        specs.append([{"type": "domain"}, {"type": "domain"}])
                fig = make_subplots(
                    rows=int((len(kernel_per_rank[kernel]) + 1) / 2),
                    cols=2,
                    specs=specs,
                )
                for rank in kernel_per_rank[kernel]:
                    fig.add_trace(
                        go.Pie(
                            labels=kernel_per_rank[kernel][rank]["name"],
                            values=kernel_per_rank[kernel][rank]["sum"],
                            title=f"Rank {rank}",
                            automargin=False,
                        ),
                        int(rank / 2) + 1,
                        int(rank % 2) + 1,
                    )
                image_size_multiplier = 1 + (len(t.traces.keys())) / 2
                fig.update_layout(
                    title_text=f'Kernel type "{kernel}" - kernel distribution on each rank',
                    margin=dict(l=50, r=50, b=50, t=50),
                    showlegend=True,
                    height=400 * image_size_multiplier,
                    legend=dict(yanchor="bottom", y=-0.1, xanchor="left", x=0),
                )
                fig.show(renderer=image_renderer)

                kernel_df = all_kernel_df[all_kernel_df["kernel_type"].eq(kernel)]

                kernel_name = kernel_df["name"].unique()
                for name in kernel_name:
                    if name != "others":
                        kernel_name_df = kernel_df[kernel_df["name"].eq(name)]
                        fig = px.bar(
                            kernel_name_df,
                            x="rank",
                            y="mean (us)",
                            title=name,
                            labels={
                                "rank": "Rank",
                                "mean (us)": "Mean Duration (us)",
                            },
                            error_y=kernel_name_df["max (us)"]
                            - kernel_name_df["mean (us)"],
                            error_y_minus=kernel_name_df["mean (us)"]
                            - kernel_name_df["min (us)"],
                        )
                        fig.update_layout(
                            title_text=f'Kernel type "{kernel}" - {name}',
                            xaxis=dict(tickmode="linear", tick0=0, dtick=1),
                        )
                        fig.show(renderer=image_renderer)

        return kernel_type_df, all_kernel_df

    @classmethod
    def _get_gpu_kernel_interval_dataframe(
        cls,
        trace_df: pd.DataFrame,
        t: "Trace",
    ) -> pd.DataFrame:
        """Obtains all GPU kernels in the trace dataframe and assigns them
        an interval index that can be used for analyzing overlap.
            @args: trace_df (pd.DataFrame) : trace df for specific rank
                Please make sure this includes "end" column.
            @args: t (Trace) : trace object

        Returns: pd.DataFrame with GPU kernels subset with an interval index
                of [start, end) intervals.
        """
        gpu_kernels_df = GPUKernelFilter()(trace_df, t.symbol_table).copy()
        gpu_kernels_intervals = pd.IntervalIndex.from_arrays(
            gpu_kernels_df["ts"], gpu_kernels_df["end"], closed="left"
        )
        gpu_kernels_df.set_index(gpu_kernels_intervals, inplace=True)
        return gpu_kernels_df

    @classmethod
    def _get_gpu_user_anno_interval_dataframe(
        cls,
        trace_df: pd.DataFrame,
        t: "Trace",
    ) -> Optional[pd.DataFrame]:
        """Obtains all GPU user annotations in the trace dataframe and assigns them
        an interval index that can be used for analyzing overlap.
            @args: trace_df (pd.DataFrame) : trace df for specific rank
                Please make sure this includes "end" column.
            @args: t (Trace) : trace object

        Returns: pd.DataFrame with GPU kernels subset with an interval index
                of [start, end) intervals.
                None if the trace does not have user annotations.
        """
        sym_id_map = t.symbol_table.get_sym_id_map()
        if (gpu_user_anno_id := sym_id_map.get("gpu_user_annotation", -1)) == -1:
            return None

        # Reverse sort annotations by duration. This order will ensure the leaf/bottom of
        # the stack is always processed last.
        gpu_user_anno_df = trace_df[trace_df.cat == gpu_user_anno_id][
            ["pid", "tid", "ts", "end", "dur", "name"]
        ].sort_values("dur", ascending=False)

        gpu_user_anno_df.set_index(
            pd.IntervalIndex.from_arrays(
                gpu_user_anno_df["ts"], gpu_user_anno_df["end"], closed="left"
            ),
            inplace=True,
        )
        return gpu_user_anno_df

    @classmethod
    def _associate_gpu_kernels_with_user_annotations(
        cls,
        trace_df: pd.DataFrame,
        gpu_kernels_df: pd.DataFrame,
        gpu_user_anno_df: pd.DataFrame,
    ) -> None:
        """Assigns each gpu_kernel  user annotation. If the kernel overlaps with multiple
        user annotations, we will pick the lowest/leaf annotation in the stack to attribute to.
            @args: trace_df (pd.DataFrame) : trace df for specific rank
                Please make sure this includes "end" column.
            @args: gpu_kernels_df (pd.DataFrame) : kernel df with interval index.
            @args: gpu_user_anno_df (pd.DataFrame) : gpu user annotation df with interval index.
        """
        # Get the pid tid combinations to scan over
        pid_tids = gpu_user_anno_df[["pid", "tid"]].drop_duplicates().to_dict("records")

        for p in pid_tids:
            pid, tid = p["pid"], p["tid"]
            gpu_user_anno_df_filt = gpu_user_anno_df.query(
                f"pid == {pid} and tid == {tid}"
            )
            logger.info(
                f"Pid,tid = {p}, Num gpu annotations = {len(gpu_user_anno_df_filt)}"
            )

            # Loop over all GPU user annotation intervals and match them with GPU
            # kernel intervals. This will be efficient if len(user annotations) << len(kernels)
            for row in gpu_user_anno_df_filt.itertuples():
                interval, anno_name = row.Index, row.name
                overlaps = gpu_kernels_df.index.overlaps(interval)
                gpu_kernels_df.loc[
                    (gpu_kernels_df["pid"] == pid)
                    & (gpu_kernels_df["tid"] == tid)
                    & overlaps,
                    "user_annotation",
                ] = anno_name

    @classmethod
    def get_gpu_kernels_with_user_annotations(
        cls,
        t: "Trace",
        rank: int,
        expand_names: bool = True,
        shortern_names: bool = True,
    ) -> Optional[pd.DataFrame]:
        """Returns a dataframe of all GPU kernels and associates them to closest or leaf
        GPU user annotation. If the kernel overlaps with multiple user annotations,
        we will pick the lowest/leaf annotation in the stack to attribute to.
        Read more in get_gpu_kernels_with_user_annotations in hta/trace_analysis.py."""
        trace_df = t.get_trace(rank)
        trace_df["end"] = trace_df["ts"] + trace_df["dur"]
        trace_df["user_annotation"] = -1

        gpu_user_anno_df = cls._get_gpu_user_anno_interval_dataframe(trace_df, t)
        if gpu_user_anno_df is None:
            logger.warning(
                f"Trace for rank {rank} does not contain any GPU user annotations"
            )
            return None

        gpu_kernels_df = cls._get_gpu_kernel_interval_dataframe(trace_df, t)

        cls._associate_gpu_kernels_with_user_annotations(
            trace_df, gpu_kernels_df, gpu_user_anno_df
        )

        if expand_names:
            decode_symbol_id_to_symbol_name(
                gpu_kernels_df, t.symbol_table, shortern_names
            )

        return gpu_kernels_df.reset_index(drop=True)

    @classmethod
    def get_gpu_user_annotation_breakdown(
        cls,
        t: "Trace",
        use_gpu_annotation: bool = True,
        visualize: bool = True,
        duration_ratio: float = 0.8,
        num_kernels: int = 1000,
        allowlist_patterns: Optional[List[str]] = None,
        image_renderer: Optional[str] = None,
    ) -> Optional[pd.DataFrame]:
        """
        Summarizes the time spent by each GPU user annotation. Outputs the following graphs:

        1. Pie charts showing the most time consuming user annotations for each rank.
        2. Bar graphs showing the average duration for the most time user annotations for each rank.

        Args:
            use_gpu_annotation (boolean): Use time on GPU for each user annotation, if false use the time on CPU instead. Default = True,
            visualize (boolean): Set to True to display the graphs. Default = True.
            duration_ratio (float): Floating point value between 0 and 1 specifying the ratio of time taken
                                    by top user annotations. Default = 0.8.
            num_kernels (int): Maximum number of user annotations to show. Default = 1000. Rest get grouped into "other".
            allowlist_patterns (list(str)): if user annotations match any of the patterns in this list, they will not be aggregated into "other" catgory. This argument is meant to keep some events as distinct in the aggregation. Supports strings as well as regular expressions.
            image_renderer (str): Set to ``notebook`` when using jupyter and ``jupyterlab`` when using jupyter-lab.
                To see all available options execute: ``import plotly; plotly.io.renderers`` in a python shell.

        Returns:
            Optional[pd.DataFrame]
                Returns a dataframe that shows the min, max, mean, standard deviation, total time taken by each
                user annotation on each rank. This dataframe will be summarized based on values of ``duration_ratio``
                and ``num_kernels``. If both ``duration_ratio`` and ``num_kernels`` are specified,
                ``num_kernels`` takes precedence.
                If user_annotations are not present on CPU or GPU (according to use_gpu_annotation flag), return None.
        """
        annotation = "gpu_user_annotation" if use_gpu_annotation else "user_annotation"
        image_renderer = image_renderer or ""

        if (idx := t.symbol_table.sym_index.get(annotation, None)) is None:
            logger.warning(f"Trace does not contain any {annotation}")
            return None

        all_kernel_df = pd.DataFrame(
            {
                "name": pd.Series(dtype="str"),
                "sum": pd.Series(dtype="int"),
                "max": pd.Series(dtype="int"),
                "min": pd.Series(dtype="int"),
                "std": pd.Series(dtype="float"),
                "mean": pd.Series(dtype="int"),
                "rank": pd.Series(dtype="int"),
            }
        )
        allowlist_names: Optional[List[str]] = None
        if allowlist_patterns is not None:
            allowlist_names = t.symbol_table.find_matched_symbols(allowlist_patterns)

        kernel_per_rank: Dict[int, pd.DataFrame] = {}

        for rank, trace_df in t.traces.items():
            gpu_user_annotation_kernels = trace_df[trace_df["cat"].eq(idx)].copy()
            t.symbol_table.add_symbols_to_trace_df(gpu_user_annotation_kernels, "name")
            logger.info(
                f"rank = {rank}, num {annotation}s = {len(gpu_user_annotation_kernels)}"
            )

            gpu_kernel_time = cls._aggr_gpu_kernel_time(
                gpu_user_annotation_kernels,
                duration_ratio=duration_ratio,
                num_kernels=num_kernels,
                allowlist_names=allowlist_names,
            )
            gpu_kernel_time["rank"] = int(rank)
            kernel_per_rank[rank] = gpu_kernel_time

            # Create all kernel info dataframe
            all_kernel_df = pd.concat(
                [all_kernel_df, gpu_kernel_time], ignore_index=True
            )

        all_kernel_df.sort_values(by=["rank", "name"], ignore_index=True, inplace=True)
        all_kernel_df.rename(
            columns={
                "sum": "sum (us)",
                "mean": "mean (us)",
                "max": "max (us)",
                "min": "min (us)",
                "std": "stddev",
            },
            inplace=True,
        )

        if visualize:  # pragma: no cover
            specs = []
            for count, rank in enumerate(kernel_per_rank):
                if count % 2 == 0:
                    specs.append([{"type": "domain"}, {"type": "domain"}])
            fig = make_subplots(
                rows=int((len(kernel_per_rank) + 1) / 2),
                cols=2,
                specs=specs,
            )
            for rank in kernel_per_rank:
                fig.add_trace(
                    go.Pie(
                        labels=kernel_per_rank[rank]["name"],
                        values=kernel_per_rank[rank]["sum"],
                        title=f"Rank {rank}",
                        automargin=False,
                    ),
                    int(rank / 2) + 1,
                    int(rank % 2) + 1,
                )
            image_size_multiplier = 1 + (len(t.traces.keys())) / 2
            fig.update_layout(
                title_text="User annotation distribution on each rank",
                margin=dict(l=50, r=50, b=50, t=50),
                showlegend=True,
                height=400 * image_size_multiplier,
                legend=dict(yanchor="bottom", y=-0.1, xanchor="left", x=0),
            )
            fig.show(renderer=image_renderer)

            kernel_name = all_kernel_df["name"].unique()
            for name in kernel_name:
                if name == "others":
                    continue
                kernel_name_df = all_kernel_df[all_kernel_df["name"].eq(name)]
                fig = px.bar(
                    kernel_name_df,
                    x="rank",
                    y="mean (us)",
                    title=name,
                    labels={
                        "rank": "Rank",
                        "mean (us)": "Mean Duration (us)",
                    },
                    error_y=kernel_name_df["max (us)"] - kernel_name_df["mean (us)"],
                    error_y_minus=kernel_name_df["mean (us)"]
                    - kernel_name_df["min (us)"],
                )
                fig.update_layout(
                    title_text=f"User annotation = {name}",
                    xaxis=dict(tickmode="linear", tick0=0, dtick=1),
                )
                fig.show(renderer=image_renderer)

        return all_kernel_df

    @classmethod
    def _get_gpu_kernel_type_time(
        cls, gpu_kernels: pd.DataFrame, kernel_type_to_analysis: List[str]
    ) -> pd.DataFrame:
        overlap_kernel_type_df = pd.DataFrame(
            {
                "status": pd.Series(dtype="str"),
                "time": pd.Series(dtype="int"),
            }
        )

        kernel_t_mapping: Dict[str, int] = defaultdict(int)
        for idx, kernel_type in enumerate(kernel_type_to_analysis):
            value = 1 << idx
            kernel_t_mapping[kernel_type] = value
            kernel_t_df = merge_kernel_intervals(
                gpu_kernels[gpu_kernels["kernel_type"].eq(kernel_type)].copy()
            )

            overlap_kernel_type_df = (
                pd.concat(
                    [
                        overlap_kernel_type_df,
                        kernel_t_df.melt(var_name="status", value_name="time").replace(
                            {"ts": value, "end": -value}
                        ),
                    ]
                )
                .sort_values(by="time")
                .reset_index(drop=True)
            )

        overlap_kernel_type_df["running"] = overlap_kernel_type_df["status"].cumsum()
        overlap_kernel_type_df["next_time"] = overlap_kernel_type_df["time"].shift(-1)
        unique_running = overlap_kernel_type_df["running"].unique()
        running_mapping: Dict[int, str] = defaultdict(str)
        for u_running in unique_running:
            if u_running > 0:
                for k_t, v_t in kernel_t_mapping.items():
                    if u_running & v_t:
                        if u_running not in running_mapping:
                            running_mapping[u_running] = k_t
                        else:
                            # FIXME linter mismatch between fbcode and git T183519933
                            # fmt: off
                            running_mapping[u_running] = (
                                f"{running_mapping[u_running]} overlapping {k_t}"
                            )
                            # fmt: on

        overlap_kernel_type_df["kernel_type"] = ""
        overlap_kernel_type_df = overlap_kernel_type_df[
            overlap_kernel_type_df["running"] > 0
        ]
        for running in running_mapping:
            overlap_kernel_type_df.loc[
                overlap_kernel_type_df["running"].eq(running), "kernel_type"
            ] = running_mapping[running]
        overlap_kernel_type_df["dur"] = (
            overlap_kernel_type_df["next_time"] - overlap_kernel_type_df["time"]
        ).astype(int)

        overlap_kernel_type_df = overlap_kernel_type_df.groupby(by=["kernel_type"])[
            "dur"
        ].agg(["sum"])
        overlap_kernel_type_df.reset_index(inplace=True)

        return overlap_kernel_type_df

    @classmethod
    def _aggr_gpu_kernel_time(
        cls,
        gpu_kernel_time: pd.DataFrame,
        num_kernels: int = 10,
        duration_ratio: float = 0.8,
        allowlist_names: Optional[List[str]] = None,
    ) -> pd.DataFrame:
        """
        Aggregates GPU kernel/events

            @args: gpu_kernel_time: flat dataframe of events to consider
            @args: num_kernels (int) : Max number of kernels to show in result. If the
                aggregate exceeds this the rest of the kernels are grouped into "other",
                by first sorting by duration in descending order.
            @args: duration_ratio (float) : a quantile threshold above which kernels are grouped
                into "other" category. For example, setting to 0.8 will result is all kernels
                past > p80 to be grouped together.
            @args: allowlist_names (list(str): if kernel names are in this list, they will not be aggregated into "other" catgory.
                This argument is meant to keep some kernel/events as distinct in the aggregation

        Returns:
            aggregated pd.DataFrame by "name" column with ["sum", "max", "min", "mean", "std"]
        """

        gpu_kernel_time = gpu_kernel_time.groupby(by=["name"])["dur"].agg(
            ["sum", "max", "min", "mean", "std"]
        )
        gpu_kernel_time.reset_index(inplace=True)
        gpu_kernel_time = gpu_kernel_time.sort_values(
            by=["sum"], ascending=False, ignore_index=True
        )
        gpu_kernel_time.fillna({"std": 0}, inplace=True)

        # if there are more than num_kernels kernels, starting to aggregate kernels
        if gpu_kernel_time.shape[0] > num_kernels:
            if allowlist_names is not None:
                keep_idx = gpu_kernel_time.name.isin(allowlist_names)
            else:
                # always false
                keep_idx = gpu_kernel_time["sum"] < 0

            gpu_kernel_time["cumsum"] = gpu_kernel_time["sum"].cumsum()
            quantiles = gpu_kernel_time["cumsum"].quantile(duration_ratio)
            # FIXME linter mismatch between fbcode and git T183519933
            # fmt: off
            gpu_kernel_time.loc[~keep_idx & (gpu_kernel_time["cumsum"] > quantiles), "name"] = (
                "others"
            )
            # fmt: on
            gpu_kernel_time.loc[
                ~keep_idx & (gpu_kernel_time.index >= num_kernels), "name"
            ] = "others"
            gpu_kernel_time = gpu_kernel_time.groupby(by=["name"])["sum"].agg(
                ["sum", "max", "min", "mean", "std"]
            )
            gpu_kernel_time.reset_index(inplace=True)
            gpu_kernel_time.fillna({"std": 0}, inplace=True)

        return gpu_kernel_time

    @classmethod
    def _get_idle_time_for_kernels(cls, kernels_df: pd.DataFrame) -> Tuple[int, int]:
        """
        Compute idle time for given set of GPU kernels :
          returns :
            idle time (us) = kernel time - merged execution time of all kernels
            kernel time (us) = defined as the time difference between end of the
                         last kernel and start of the first kernel.
            PS: we exclude the last profiler iteration while reading trace
            so total time is exclusive of that.
        """
        merged_kernels = merge_kernel_intervals(kernels_df)
        kernel_time = merged_kernels.iloc[-1]["end"] - merged_kernels.iloc[0]["ts"]
        # differences of end - ts are commutative
        kernel_run_time = merged_kernels.end.sum() - merged_kernels.ts.sum()
        return kernel_time - kernel_run_time, kernel_time

    @classmethod
    def get_temporal_breakdown(cls, t: "Trace", visualize: bool = True) -> pd.DataFrame:
        """
        Temporal breakdown implementation. See `get_temporal_breakdown` in `trace_analysis.py` for details.
        """
        sym_table = t.symbol_table.get_sym_table()

        def idle_time_per_rank(trace_df: pd.DataFrame) -> Tuple[int, int, int, int]:
            """returns idle_time (us) , compute_time (us), non_compute_time (us), total_time (us)"""
            gpu_kernels = trace_df[trace_df["stream"].ne(-1)].copy()
            idle_time, kernel_time = cls._get_idle_time_for_kernels(gpu_kernels)

            gpu_kernels["kernel_type"] = gpu_kernels[["name"]].apply(
                lambda x: get_kernel_type(sym_table[x["name"]]), axis=1
            )

            # Isolate computation kernels and merge each one of them.
            comp_kernels = merge_kernel_intervals(
                gpu_kernels[
                    gpu_kernels["kernel_type"].eq(KernelType.COMPUTATION.name)
                ].copy()
            )

            comm_kernels = merge_kernel_intervals(
                gpu_kernels[
                    gpu_kernels["kernel_type"].eq(KernelType.COMMUNICATION.name)
                ].copy()
            )

            mem_kernels = merge_kernel_intervals(
                gpu_kernels[
                    gpu_kernels["kernel_type"].eq(KernelType.MEMORY.name)
                ].copy()
            )
                
            compute_time = comp_kernels.end.sum() - comp_kernels.ts.sum()
            comm_time = comm_kernels.end.sum() - comm_kernels.ts.sum()
            mem_time = mem_kernels.end.sum() - mem_kernels.ts.sum()
            non_compute_time = kernel_time - compute_time - idle_time

            assert idle_time <= kernel_time
            assert compute_time <= kernel_time
            assert non_compute_time >= 0
            assert comm_time <= kernel_time
            assert mem_time <= kernel_time

            return idle_time, compute_time, comm_time, mem_time, non_compute_time, kernel_time

        result: Dict[str, List[float]] = defaultdict(list)
        for rank, trace_df in t.traces.items():
            result["rank"].append(rank)
            idle_time, compute_time, comm_time, mem_time, non_compute_time, kernel_time = idle_time_per_rank(
                trace_df
            )
            result["idle_time(us)"].append(idle_time)
            result["compute_time(us)"].append(compute_time)
            result["non_compute_time(us)"].append(non_compute_time)
            result["kernel_time(us)"].append(kernel_time)
            result["comm_time(us)"].append(comm_time)
            result["mem_time(us)"].append(mem_time)

        result_df = pd.DataFrame(result)
        result_df["idle_time"] = (
            result_df["idle_time(us)"] / result_df["kernel_time(us)"]
        )
        result_df["idle_time_pctg"] = round(100 * result_df["idle_time"], 2)

        result_df["compute_time"] = (
            result_df["compute_time(us)"] / result_df["kernel_time(us)"]
        )
        result_df["compute_time_pctg"] = round(100 * result_df["compute_time"], 2)

        result_df["comm_time"] = (
            result_df["comm_time(us)"] / result_df["kernel_time(us)"]
        )
        result_df["comm_time_pctg"] = round(100 * result_df["comm_time"], 2)
        
        result_df["mem_time"] = (
            result_df["mem_time(us)"] / result_df["kernel_time(us)"]
        )
        result_df["mem_time_pctg"] = round(100 * result_df["mem_time"], 2)

        if visualize:  # pragma: no cover
            fig = px.bar(
                result_df,
                x="rank",
                y=["idle_time", "compute_time", "comm_time", "mem_time"],
                title="Temporal breakdown across ranks",
                labels={
                    "rank": "Rank",
                },
            )
            fig.update_layout(
                yaxis_tickformat=".2%",
                yaxis_title="Percentage",
                legend_title="Time Breakdown",
            )
            fig.show()

        return result_df[
            [
                "rank",
                "idle_time(us)",
                "compute_time(us)",
                "comm_time(us)",
                "mem_time(us)",
                "non_compute_time(us)",
                "kernel_time(us)",
                "idle_time_pctg",
                "compute_time_pctg",
                "comm_time_pctg",
                "mem_time_pctg",
                "non_compute_time_pctg",
            ]
        ]

    @classmethod
    def _analyze_idle_time_for_stream(
        cls,
        stream: int,
        gpu_kernels: pd.DataFrame,
        consecutive_kernel_delay: int,
        show_idle_interval_stats=False,
    ) -> Tuple[pd.DataFrame, Optional[pd.DataFrame]]:
        """Analyze a specific CUDA stream for idle time breakdown on it.

        stream (int): CUDA stream to consider.
        gpu_kernels: a dataframe of GPU kernels in a rank.

        returns
        1) dataframe with idle time breakdown.
        1) optional dataframe showing idle interval statistics.
        """
        logger.info(f"Processing stream {stream}")
        idle_interval_stats: Optional[pd.DataFrame] = None

        gpu_kernels_s = (
            gpu_kernels[gpu_kernels.stream == stream].copy().sort_values(by="ts")
        )

        gpu_kernels_s["end_ts"] = gpu_kernels_s.ts + gpu_kernels_s.dur
        gpu_kernels_s["prev_end_ts"] = gpu_kernels_s.end_ts.shift(1)
        gpu_kernels_s["idle_interval"] = (
            gpu_kernels_s["ts"] - gpu_kernels_s["prev_end_ts"]
        )

        # Default idle time category
        gpu_kernels_s["idle_category"] = IdleTimeType.OTHER.value

        """
        Host wait:
            If the current kernel's runtime started after previous kernel's end time
            this means the Host/CPU was not enqueuing kernels fast enough.
        CPU  Runtime 0             Runtime 1
        GPU     |--------Kernel 0      |-----------Kernel 1
        """
        is_host_wait = gpu_kernels_s["ts_runtime"] > gpu_kernels_s["prev_end_ts"]
        gpu_kernels_s.loc[is_host_wait, "idle_category"] = IdleTimeType.HOST_WAIT.value

        """
        Kernel wait:
            If the gap between kernels is below a threshold the idle time is
            likely due to the overhead for launching kernels.
        """
        is_kernel_kernel_delay = ~is_host_wait & (
            gpu_kernels_s["idle_interval"] < consecutive_kernel_delay
        )
        # FIXME linter mismatch between fbcode and git T183519933
        # fmt: off
        gpu_kernels_s.loc[is_kernel_kernel_delay, "idle_category"] = (
            IdleTimeType.KERNEL_WAIT.value
        )
        # fmt: on

        gpu_kernels_groupby = gpu_kernels_s.groupby("idle_category")
        if show_idle_interval_stats:
            logger.info(
                f"Computing descriptive statistics for idle time intervals on stream {stream}:"
            )
            idle_interval_stats = gpu_kernels_groupby.idle_interval.describe()
            idle_interval_stats.insert(0, "stream", stream)

        result = pd.DataFrame(gpu_kernels_groupby.idle_interval.sum())
        total_idle_time = result.idle_interval.sum()
        result["stream"] = stream
        result["idle_time_ratio"] = result["idle_interval"] / total_idle_time
        result.rename(columns={"idle_interval": "idle_time"}, inplace=True)
        return result, idle_interval_stats

    @classmethod
    def get_idle_time_breakdown(
        cls,
        t: "Trace",
        consecutive_kernel_delay: int,
        rank: int = 0,
        streams: Optional[List[int]] = None,
        visualize: bool = True,
        visualize_pctg: bool = True,
        show_idle_interval_stats=False,
    ) -> Tuple[pd.DataFrame, Optional[pd.DataFrame]]:
        """
        Breakdown Idle time by host wait, kernel wait and other categories. See full description in trace_analysis.py

        consecutive_kernel_delay (int): configures the threshold under which we consider gaps between
           kernels to be due to realistic delays in launching back-back kernels on the GPU. Time is in ns.
        rank (int): the rank to analyze
        streams (List[int]): list of streams to provide analysis for.
            Defaults to all streams.
        visualize (bool): show the visualization chart or not (default = True).
        visualize_pctg (bool): show relative percentage across streams (default = True).
        show_idle_interval_stats (bool): prints statistics of the idle intervals like the min, max
           and median of idle intervals between kernels on a CUDA stream, also broken down by
           the idleness category (default = False).
        """
        trace_df: pd.DataFrame = t.get_trace(rank)

        # Need to filter out events with `cuda_sync` category
        kernel_cats = [
            "kernel",
            "Kernel",
            "gpu_memset",
            "Memset",
            "gpu_memcpy",
            "Memcpy",
            "mtia_ccp_events",
        ]
        sym_id_map = t.symbol_table.get_sym_id_map()
        kernel_cat_ids = [sym_id_map.get(cat, -1000) for cat in kernel_cats]

        gpu_kernels_pre = (
            trace_df[trace_df["stream"].ne(-1) & trace_df["cat"].isin(kernel_cat_ids)]
            .copy()
            .set_index("index_correlation")
        )

        # correlate with the runtime event whenever possible
        gpu_kernels = gpu_kernels_pre.join(
            trace_df[["ts", "index"]], on="index_correlation", rsuffix="_runtime"
        )

        if streams is None or len(streams) == 0:
            streams = list(gpu_kernels.stream.unique())

        result_list: List[pd.DataFrame] = []
        interval_stats_list: List[pd.DataFrame] = []

        for stream in streams:
            breakdown_df, idle_interval_df = cls._analyze_idle_time_for_stream(
                stream,
                gpu_kernels,
                consecutive_kernel_delay,
                show_idle_interval_stats,
            )
            result_list.append(breakdown_df)
            if idle_interval_df is not None:
                interval_stats_list.append(idle_interval_df)

        result_df = pd.concat(result_list)

        idle_category_name_map = {
            member.value: name.lower()
            for name, member in IdleTimeType.__members__.items()
        }
        result_df.rename(mapper=idle_category_name_map, axis=0, inplace=True)
        result_df.reset_index(inplace=True)

        if visualize:  # pragma: no cover
            result_df["stream"] = result_df.stream.astype(str)
            ycol = "idle_time_ratio" if visualize_pctg else "idle_time"
            fig = px.bar(
                result_df,
                x="stream",
                y=ycol,
                color="idle_category",
                hover_data=["idle_time", "idle_time_ratio"],
                title=f"Idle time breakdown on rank {rank} per CUDA stream",
            )
            if visualize_pctg:
                fig.update_layout(
                    yaxis_tickformat=".2%",
                    yaxis_title="Percentage",
                    legend_title="Idle Time Breakdown",
                )
            else:
                fig.update_layout(
                    yaxis_title="Idle time (us)", legend_title="Idle Time Breakdown"
                )
            fig.show()

        result_df["rank"] = rank
        interval_stats_df = (
            pd.concat(interval_stats_list).round(2)
            if show_idle_interval_stats
            else None
        )
        if interval_stats_df is not None:
            # add rank column to the starting
            interval_stats_df.insert(0, "rank", rank)
            interval_stats_df.rename(
                mapper=idle_category_name_map, axis=0, inplace=True
            )

        result_df = result_df[
            ["rank", "stream", "idle_category", "idle_time", "idle_time_ratio"]
        ].round(2)

        return result_df, interval_stats_df
