import logging
import traceback
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Dict, List, Optional, Set, Tuple, Union
from urllib.parse import urlparse

import humanfriendly
import redshift_connector

import datahub.emitter.mce_builder as builder
import datahub.utilities.sqlglot_lineage as sqlglot_l
from datahub.emitter import mce_builder
from datahub.emitter.mce_builder import make_dataset_urn_with_platform_instance
from datahub.ingestion.api.common import PipelineContext
from datahub.ingestion.source.aws.s3_util import strip_s3_prefix
from datahub.ingestion.source.redshift.config import LineageMode, RedshiftConfig
from datahub.ingestion.source.redshift.query import RedshiftQuery
from datahub.ingestion.source.redshift.redshift_schema import (
    LineageRow,
    RedshiftDataDictionary,
    RedshiftSchema,
    RedshiftTable,
    RedshiftView,
)
from datahub.ingestion.source.redshift.report import RedshiftReport
from datahub.ingestion.source.state.redundant_run_skip_handler import (
    RedundantLineageRunSkipHandler,
)
from datahub.metadata.com.linkedin.pegasus2avro.dataset import (
    FineGrainedLineage,
    FineGrainedLineageDownstreamType,
    FineGrainedLineageUpstreamType,
    UpstreamLineage,
)
from datahub.metadata.schema_classes import (
    DatasetLineageTypeClass,
    UpstreamClass,
    UpstreamLineageClass,
)
from datahub.utilities import memory_footprint
from datahub.utilities.dedup_list import deduplicate_list
from datahub.utilities.urns import dataset_urn

logger: logging.Logger = logging.getLogger(__name__)


class LineageDatasetPlatform(Enum):
    S3 = "s3"
    REDSHIFT = "redshift"


class LineageCollectorType(Enum):
    QUERY_SCAN = "query_scan"
    QUERY_SQL_PARSER = "query_sql_parser"
    VIEW = "view"
    VIEW_DDL_SQL_PARSING = "view-ddl-sql-parsing"
    COPY = "copy"
    UNLOAD = "unload"


@dataclass(frozen=True, eq=True)
class LineageDataset:
    platform: LineageDatasetPlatform
    urn: str


@dataclass()
class LineageItem:
    dataset: LineageDataset
    upstreams: Set[LineageDataset]
    cll: Optional[List[sqlglot_l.ColumnLineageInfo]]
    collector_type: LineageCollectorType
    dataset_lineage_type: str = field(init=False)

    def __post_init__(self):
        if self.collector_type == LineageCollectorType.COPY:
            self.dataset_lineage_type = DatasetLineageTypeClass.COPY
        elif self.collector_type in [
            LineageCollectorType.VIEW,
            LineageCollectorType.VIEW_DDL_SQL_PARSING,
        ]:
            self.dataset_lineage_type = DatasetLineageTypeClass.VIEW
        else:
            self.dataset_lineage_type = DatasetLineageTypeClass.TRANSFORMED

    def merge_lineage(
        self,
        upstreams: Set[LineageDataset],
        cll: Optional[List[sqlglot_l.ColumnLineageInfo]],
    ) -> None:
        self.upstreams = self.upstreams.union(upstreams)

        # Merge CLL using the output column name as the merge key.
        self.cll = self.cll or []
        existing_cll: Dict[str, sqlglot_l.ColumnLineageInfo] = {
            c.downstream.column: c for c in self.cll
        }
        for c in cll or []:
            if c.downstream.column in existing_cll:
                # Merge using upstream + column name as the merge key.
                existing_cll[c.downstream.column].upstreams = deduplicate_list(
                    [*existing_cll[c.downstream.column].upstreams, *c.upstreams]
                )
            else:
                # New output column, just add it as is.
                self.cll.append(c)

        self.cll = self.cll or None


class RedshiftLineageExtractor:
    def __init__(
        self,
        config: RedshiftConfig,
        report: RedshiftReport,
        context: PipelineContext,
        redundant_run_skip_handler: Optional[RedundantLineageRunSkipHandler] = None,
    ):
        self.config = config
        self.report = report
        self.context = context
        self._lineage_map: Dict[str, LineageItem] = defaultdict()

        self.redundant_run_skip_handler = redundant_run_skip_handler
        self.start_time, self.end_time = (
            self.report.lineage_start_time,
            self.report.lineage_end_time,
        ) = self.get_time_window()

    def get_time_window(self) -> Tuple[datetime, datetime]:
        if self.redundant_run_skip_handler:
            self.report.stateful_lineage_ingestion_enabled = True
            return self.redundant_run_skip_handler.suggest_run_time_window(
                self.config.start_time, self.config.end_time
            )
        else:
            return self.config.start_time, self.config.end_time

    def warn(self, log: logging.Logger, key: str, reason: str) -> None:
        self.report.report_warning(key, reason)
        log.warning(f"{key} => {reason}")

    def _get_s3_path(self, path: str) -> str:
        if self.config.s3_lineage_config:
            for path_spec in self.config.s3_lineage_config.path_specs:
                if path_spec.allowed(path):
                    _, table_path = path_spec.extract_table_name_and_path(path)
                    return table_path

            if self.config.s3_lineage_config.strip_urls:
                if "/" in urlparse(path).path:
                    return str(path.rsplit("/", 1)[0])

        return path

    def _get_sources_from_query(
        self, db_name: str, query: str
    ) -> Tuple[List[LineageDataset], Optional[List[sqlglot_l.ColumnLineageInfo]]]:
        sources: List[LineageDataset] = list()

        parsed_result: Optional[
            sqlglot_l.SqlParsingResult
        ] = sqlglot_l.create_lineage_sql_parsed_result(
            query=query,
            platform=LineageDatasetPlatform.REDSHIFT.value,
            platform_instance=self.config.platform_instance,
            default_db=db_name,
            default_schema=str(self.config.default_schema),
            graph=self.context.graph,
            env=self.config.env,
        )

        if parsed_result is None:
            logger.debug(f"native query parsing failed for {query}")
            return sources, None

        logger.debug(f"parsed_result = {parsed_result}")

        for table_urn in parsed_result.in_tables:
            source = LineageDataset(
                platform=LineageDatasetPlatform.REDSHIFT,
                urn=table_urn,
            )
            sources.append(source)

        return (
            sources,
            parsed_result.column_lineage
            if self.config.include_view_column_lineage
            else None,
        )

    def _build_s3_path_from_row(self, filename: str) -> str:
        path = filename.strip()
        if urlparse(path).scheme != "s3":
            raise ValueError(
                f"Only s3 source supported with copy/unload. The source was: {path}"
            )
        return strip_s3_prefix(self._get_s3_path(path))

    def _get_sources(
        self,
        lineage_type: LineageCollectorType,
        db_name: str,
        source_schema: Optional[str],
        source_table: Optional[str],
        ddl: Optional[str],
        filename: Optional[str],
    ) -> Tuple[List[LineageDataset], Optional[List[sqlglot_l.ColumnLineageInfo]]]:
        sources: List[LineageDataset] = list()
        # Source
        cll: Optional[List[sqlglot_l.ColumnLineageInfo]] = None

        if (
            lineage_type
            in {
                lineage_type.QUERY_SQL_PARSER,
                lineage_type.VIEW_DDL_SQL_PARSING,
            }
            and ddl is not None
        ):
            try:
                sources, cll = self._get_sources_from_query(db_name=db_name, query=ddl)
            except Exception as e:
                logger.warning(
                    f"Error parsing query {ddl} for getting lineage. Error was {e}."
                )
                self.report.num_lineage_dropped_query_parser += 1
        else:
            if lineage_type == lineage_type.COPY and filename is not None:
                platform = LineageDatasetPlatform.S3
                path = filename.strip()
                if urlparse(path).scheme != "s3":
                    logger.warning(
                        "Only s3 source supported with copy. The source was: {path}."
                    )
                    self.report.num_lineage_dropped_not_support_copy_path += 1
                    return [], None
                path = strip_s3_prefix(self._get_s3_path(path))
                urn = make_dataset_urn_with_platform_instance(
                    platform=platform.value,
                    name=path,
                    env=self.config.env,
                    platform_instance=self.config.platform_instance_map.get(
                        platform.value
                    )
                    if self.config.platform_instance_map is not None
                    else None,
                )
            elif source_schema is not None and source_table is not None:
                platform = LineageDatasetPlatform.REDSHIFT
                path = f"{db_name}.{source_schema}.{source_table}"
                urn = make_dataset_urn_with_platform_instance(
                    platform=platform.value,
                    platform_instance=self.config.platform_instance,
                    name=path,
                    env=self.config.env,
                )
            else:
                return [], cll

            sources = [
                LineageDataset(
                    platform=platform,
                    urn=urn,
                )
            ]

        return sources, cll

    def _populate_lineage_map(
        self,
        query: str,
        database: str,
        lineage_type: LineageCollectorType,
        connection: redshift_connector.Connection,
        all_tables: Dict[str, Dict[str, List[Union[RedshiftView, RedshiftTable]]]],
    ) -> None:
        """
        This method generate table level lineage based with the given query.
        The query should return the following columns: target_schema, target_table, source_table, source_schema
        source_table and source_schema can be omitted if the sql_field is set because then it assumes the source_table
        and source_schema will be extracted from the sql_field by sql parsing.

        :param query: The query to run to extract lineage.
        :type query: str
        :param lineage_type: The way the lineage should be processed
        :type lineage_type: LineageType
        return: The method does not return with anything as it directly modify the self._lineage_map property.
        :rtype: None
        """
        try:
            cll: Optional[List[sqlglot_l.ColumnLineageInfo]] = None
            raw_db_name = database
            alias_db_name = self.config.database

            for lineage_row in RedshiftDataDictionary.get_lineage_rows(
                conn=connection, query=query
            ):
                target = self._get_target_lineage(
                    alias_db_name, lineage_row, lineage_type
                )
                if not target:
                    continue

                sources, cll = self._get_sources(
                    lineage_type,
                    alias_db_name,
                    source_schema=lineage_row.source_schema,
                    source_table=lineage_row.source_table,
                    ddl=lineage_row.ddl,
                    filename=lineage_row.filename,
                )

                target.upstreams.update(
                    self._get_upstream_lineages(
                        sources=sources,
                        all_tables=all_tables,
                        alias_db_name=alias_db_name,
                        raw_db_name=raw_db_name,
                    )
                )
                target.cll = cll

                # Merging upstreams if dataset already exists and has upstreams
                if target.dataset.urn in self._lineage_map:
                    self._lineage_map[target.dataset.urn].merge_lineage(
                        upstreams=target.upstreams, cll=target.cll
                    )
                else:
                    self._lineage_map[target.dataset.urn] = target

                logger.debug(
                    f"Lineage[{target}]:{self._lineage_map[target.dataset.urn]}"
                )
        except Exception as e:
            self.warn(
                logger,
                f"extract-{lineage_type.name}",
                f"Error was {e}, {traceback.format_exc()}",
            )
            self.report_status(f"extract-{lineage_type.name}", False)

    def _get_target_lineage(
        self,
        alias_db_name: str,
        lineage_row: LineageRow,
        lineage_type: LineageCollectorType,
    ) -> Optional[LineageItem]:
        if (
            lineage_type != LineageCollectorType.UNLOAD
            and lineage_row.target_schema
            and lineage_row.target_table
        ):
            if not self.config.schema_pattern.allowed(
                lineage_row.target_schema
            ) or not self.config.table_pattern.allowed(
                f"{alias_db_name}.{lineage_row.target_schema}.{lineage_row.target_table}"
            ):
                return None
        # Target
        if lineage_type == LineageCollectorType.UNLOAD and lineage_row.filename:
            try:
                target_platform = LineageDatasetPlatform.S3
                # Following call requires 'filename' key in lineage_row
                target_path = self._build_s3_path_from_row(lineage_row.filename)
                urn = make_dataset_urn_with_platform_instance(
                    platform=target_platform.value,
                    name=target_path,
                    env=self.config.env,
                    platform_instance=self.config.platform_instance_map.get(
                        target_platform.value
                    )
                    if self.config.platform_instance_map is not None
                    else None,
                )
            except ValueError as e:
                self.warn(logger, "non-s3-lineage", str(e))
                return None
        else:
            target_platform = LineageDatasetPlatform.REDSHIFT
            target_path = f"{alias_db_name}.{lineage_row.target_schema}.{lineage_row.target_table}"
            urn = make_dataset_urn_with_platform_instance(
                platform=target_platform.value,
                platform_instance=self.config.platform_instance,
                name=target_path,
                env=self.config.env,
            )

        return LineageItem(
            dataset=LineageDataset(platform=target_platform, urn=urn),
            upstreams=set(),
            collector_type=lineage_type,
            cll=None,
        )

    def _get_upstream_lineages(
        self,
        sources: List[LineageDataset],
        all_tables: Dict[str, Dict[str, List[Union[RedshiftView, RedshiftTable]]]],
        alias_db_name: str,
        raw_db_name: str,
    ) -> List[LineageDataset]:
        targe_source = []
        for source in sources:
            if source.platform == LineageDatasetPlatform.REDSHIFT:
                qualified_table_name = dataset_urn.DatasetUrn.create_from_string(
                    source.urn
                ).get_entity_id()[1]
                # -3 because platform instance is optional and that can cause the split to have more than 3 elements
                db, schema, table = qualified_table_name.split(".")[-3:]
                if db == raw_db_name:
                    db = alias_db_name
                    path = f"{db}.{schema}.{table}"
                    source = LineageDataset(
                        platform=source.platform,
                        urn=make_dataset_urn_with_platform_instance(
                            platform=LineageDatasetPlatform.REDSHIFT.value,
                            platform_instance=self.config.platform_instance,
                            name=path,
                            env=self.config.env,
                        ),
                    )

                # Filtering out tables which does not exist in Redshift
                # It was deleted in the meantime or query parser did not capture well the table name
                if (
                    db not in all_tables
                    or schema not in all_tables[db]
                    or not any(table == t.name for t in all_tables[db][schema])
                ):
                    logger.debug(
                        f"{source.urn} missing table, dropping from lineage.",
                    )
                    self.report.num_lineage_tables_dropped += 1
                    continue

            targe_source.append(source)
        return targe_source

    def populate_lineage(
        self,
        database: str,
        connection: redshift_connector.Connection,
        all_tables: Dict[str, Dict[str, List[Union[RedshiftView, RedshiftTable]]]],
    ) -> None:
        populate_calls: List[Tuple[str, LineageCollectorType]] = []

        if self.config.table_lineage_mode in {
            LineageMode.STL_SCAN_BASED,
            LineageMode.MIXED,
        }:
            # Populate table level lineage by getting upstream tables from stl_scan redshift table
            query = RedshiftQuery.stl_scan_based_lineage_query(
                self.config.database,
                self.start_time,
                self.end_time,
            )
            populate_calls.append((query, LineageCollectorType.QUERY_SCAN))
        if self.config.table_lineage_mode in {
            LineageMode.SQL_BASED,
            LineageMode.MIXED,
        }:
            # Populate table level lineage by parsing table creating sqls
            query = RedshiftQuery.list_insert_create_queries_sql(
                db_name=database,
                start_time=self.start_time,
                end_time=self.end_time,
            )
            populate_calls.append((query, LineageCollectorType.QUERY_SQL_PARSER))

        if self.config.include_views and self.config.include_view_lineage:
            # Populate table level lineage for views
            query = RedshiftQuery.view_lineage_query()
            populate_calls.append((query, LineageCollectorType.VIEW))

            # Populate table level lineage for late binding views
            query = RedshiftQuery.list_late_view_ddls_query()
            populate_calls.append((query, LineageCollectorType.VIEW_DDL_SQL_PARSING))

        if self.config.include_copy_lineage:
            query = RedshiftQuery.list_copy_commands_sql(
                db_name=database,
                start_time=self.start_time,
                end_time=self.end_time,
            )
            populate_calls.append((query, LineageCollectorType.COPY))

        if self.config.include_unload_lineage:
            query = RedshiftQuery.list_unload_commands_sql(
                db_name=database,
                start_time=self.start_time,
                end_time=self.end_time,
            )

            populate_calls.append((query, LineageCollectorType.UNLOAD))

        for query, lineage_type in populate_calls:
            self._populate_lineage_map(
                query=query,
                database=database,
                lineage_type=lineage_type,
                connection=connection,
                all_tables=all_tables,
            )

        self.report.lineage_mem_size[self.config.database] = humanfriendly.format_size(
            memory_footprint.total_size(self._lineage_map)
        )

    def make_fine_grained_lineage_class(
        self, lineage_item: LineageItem, dataset_urn: str
    ) -> List[FineGrainedLineage]:
        fine_grained_lineages: List[FineGrainedLineage] = []

        if (
            self.config.extract_column_level_lineage is False
            or lineage_item.cll is None
        ):
            logger.debug("CLL extraction is disabled")
            return fine_grained_lineages

        logger.debug("Extracting column level lineage")

        cll: List[sqlglot_l.ColumnLineageInfo] = lineage_item.cll

        for cll_info in cll:
            downstream = (
                [builder.make_schema_field_urn(dataset_urn, cll_info.downstream.column)]
                if cll_info.downstream is not None
                and cll_info.downstream.column is not None
                else []
            )

            upstreams = [
                builder.make_schema_field_urn(column_ref.table, column_ref.column)
                for column_ref in cll_info.upstreams
            ]

            fine_grained_lineages.append(
                FineGrainedLineage(
                    downstreamType=FineGrainedLineageDownstreamType.FIELD,
                    downstreams=downstream,
                    upstreamType=FineGrainedLineageUpstreamType.FIELD_SET,
                    upstreams=upstreams,
                )
            )

        logger.debug(f"Created fine_grained_lineage for {dataset_urn}")

        return fine_grained_lineages

    def get_lineage(
        self,
        table: Union[RedshiftTable, RedshiftView],
        dataset_urn: str,
        schema: RedshiftSchema,
    ) -> Optional[Tuple[UpstreamLineageClass, Dict[str, str]]]:
        upstream_lineage: List[UpstreamClass] = []

        cll_lineage: List[FineGrainedLineage] = []

        if dataset_urn in self._lineage_map:
            item = self._lineage_map[dataset_urn]
            for upstream in item.upstreams:
                upstream_table = UpstreamClass(
                    dataset=upstream.urn,
                    type=item.dataset_lineage_type,
                )
                upstream_lineage.append(upstream_table)

            cll_lineage = self.make_fine_grained_lineage_class(
                lineage_item=item,
                dataset_urn=dataset_urn,
            )

        tablename = table.name
        if table.type == "EXTERNAL_TABLE":
            # external_db_params = schema.option
            upstream_platform = schema.type.lower()
            catalog_upstream = UpstreamClass(
                mce_builder.make_dataset_urn_with_platform_instance(
                    upstream_platform,
                    f"{schema.external_database}.{tablename}",
                    platform_instance=self.config.platform_instance_map.get(
                        upstream_platform
                    )
                    if self.config.platform_instance_map
                    else None,
                    env=self.config.env,
                ),
                DatasetLineageTypeClass.COPY,
            )
            upstream_lineage.append(catalog_upstream)

        if upstream_lineage:
            self.report.upstream_lineage[dataset_urn] = [
                u.dataset for u in upstream_lineage
            ]
        else:
            return None

        return (
            UpstreamLineage(
                upstreams=upstream_lineage, fineGrainedLineages=cll_lineage or None
            ),
            {},
        )

    def report_status(self, step: str, status: bool) -> None:
        if self.redundant_run_skip_handler:
            self.redundant_run_skip_handler.report_current_run_status(step, status)
