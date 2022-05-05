#  Copyright 2021 Collate
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#  http://www.apache.org/licenses/LICENSE-2.0
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
import traceback
import uuid
from typing import Iterable, Optional, Union

from snowflake.sqlalchemy.custom_types import VARIANT
from snowflake.sqlalchemy.snowdialect import SnowflakeDialect, ischema_names
from sqlalchemy.engine import reflection
from sqlalchemy.engine.reflection import Inspector
from sqlalchemy.inspection import inspect
from sqlalchemy.sql import text

from metadata.config.common import TagRequest
from metadata.generated.schema.api.tags.createTag import CreateTagRequest
from metadata.generated.schema.api.tags.createTagCategory import (
    CreateTagCategoryRequest,
)
from metadata.generated.schema.entity.data.table import Table, TableData
from metadata.generated.schema.entity.services.connections.database.snowflakeConnection import (
    SnowflakeConnection,
)
from metadata.generated.schema.entity.services.connections.metadata.openMetadataConnection import (
    OpenMetadataConnection,
)
from metadata.generated.schema.metadataIngestion.databaseServiceMetadataPipeline import (
    DatabaseServiceMetadataPipeline,
)
from metadata.generated.schema.metadataIngestion.workflow import (
    Source as WorkflowSource,
)
from metadata.ingestion.api.common import Entity
from metadata.ingestion.api.source import InvalidSourceException, Source, SourceStatus
from metadata.ingestion.models.ometa_table_db import OMetaDatabaseAndTable
from metadata.ingestion.ometa.ometa_api import OpenMetadata
from metadata.ingestion.source.sql_source import SQLSource
from metadata.utils.column_type_parser import create_sqlalchemy_type
from metadata.utils.connections import get_connection
from metadata.utils.filters import filter_by_table
from metadata.utils.logger import ingestion_logger
from metadata.utils.sql_queries import FETCH_SNOWFLAKE_METADATA, FETCH_SNOWFLAKE_TAGS

GEOGRAPHY = create_sqlalchemy_type("GEOGRAPHY")
ischema_names["VARIANT"] = VARIANT
ischema_names["GEOGRAPHY"] = GEOGRAPHY

logger = ingestion_logger()


class SnowflakeSource(SQLSource):
    def __init__(self, config, metadata_config):
        super().__init__(config, metadata_config)

    def get_databases(self) -> Iterable[Inspector]:
        if self.config.serviceConnection.__root__.config.database:
            yield from super().get_databases()
        else:
            query = "SHOW DATABASES"
            results = self.connection.execute(query)
            for res in results:
                row = list(res)
                use_db_query = f"USE DATABASE {row[1]}"
                self.connection.execute(use_db_query)
                logger.info(f"Ingesting from database: {row[1]}")
                self.config.serviceConnection.__root__.config.database = row[1]
                self.engine = get_connection(self.service_connection)
                yield inspect(self.engine)

    def fetch_tags(self, schema, table: str, object_type: str = "table"):
        self.connection.execute(f"USE {self.service_connection.database}.{schema}")
        result = self.connection.execute(
            FETCH_SNOWFLAKE_TAGS.format(table, object_type)
        )
        tags = []
        for res in result:
            logger.info("Ingesting Tags")
            row = list(res)
            tag_category = row[2]
            primary_tag = row[3]
            tags.append(
                TagRequest(
                    category_name=CreateTagCategoryRequest(
                        name=tag_category,
                        description="SNOWFLAKE TAG NAME",
                        categoryType="Descriptive",
                    ),
                    category_details=CreateTagRequest(
                        name=primary_tag, description="SNOWFLAKE TAG VALUE"
                    ),
                )
            )
        return tags

    def fetch_sample_data(self, schema: str, table: str) -> Optional[TableData]:
        resp_sample_data = super().fetch_sample_data(schema, table)
        if not resp_sample_data:
            try:
                logger.info("Using Table Name with quotes to fetch the data")
                query = self.source_config.sampleDataQuery.format(schema, f'"{table}"')
                logger.info(query)
                results = self.connection.execute(query)
                cols = []
                for col in results.keys():
                    cols.append(col)
                rows = []
                for res in results:
                    row = list(res)
                    rows.append(row)
                return TableData(columns=cols, rows=rows)
            except Exception as err:
                logger.error(err)
        return resp_sample_data

    @classmethod
    def create(cls, config_dict, metadata_config: OpenMetadataConnection):
        config: WorkflowSource = WorkflowSource.parse_obj(config_dict)
        connection: SnowflakeConnection = config.serviceConnection.__root__.config
        if not isinstance(connection, SnowflakeConnection):
            raise InvalidSourceException(
                f"Expected SnowflakeConnection, but got {connection}"
            )
        return cls(config, metadata_config)

    def next_record(self) -> Iterable[Entity]:
        for inspector in self.get_databases():
            yield from self.fetch_tables(inspector=inspector, schema="")

    def fetch_tables(
        self,
        inspector: Inspector,
        schema: str,
    ) -> Iterable[Union[OMetaDatabaseAndTable, TagRequest]]:
        entities = inspector.get_table_names()
        for db, schema, entity, entity_type, comment in entities:
            try:
                if filter_by_table(
                    self.source_config.tableFilterPattern, table_name=entity
                ):
                    self.status.filter(
                        f"{self.config.serviceName}.{db}.{schema}.{entity}",
                        "{} pattern not allowed".format(entity_type),
                    )
                    continue
                table_columns = self._get_columns(schema, entity, inspector)
                view_definition = inspector.get_view_definition(entity, schema)
                view_definition = (
                    "" if view_definition is None else str(view_definition)
                )
                table_entity = Table(
                    id=uuid.uuid4(),
                    name=entity,
                    tableType="Regular" if entity_type == "Base Table" else "View",
                    description=comment,
                    columns=table_columns,
                    viewDefinition=view_definition,
                )
                if self.source_config.generateSampleData:
                    table_data = self.fetch_sample_data(schema, entity)
                    table_entity.sampleData = table_data
                if self.source_config.enableDataProfiler:
                    profile = self.run_profiler(table=entity, schema=schema)
                    table_entity.tableProfile = [profile] if profile else None
                database = self._get_database(self.service_connection.database)
                table_schema_and_db = OMetaDatabaseAndTable(
                    table=table_entity,
                    database=database,
                    database_schema=self._get_schema(schema, database),
                )
                self.register_record(table_schema_and_db)
                yield table_schema_and_db
            except Exception as err:
                logger.debug(traceback.format_exc())
                logger.error(err)


def get_table_names(self, connection, schema=None, **kw):
    result = connection.execute(FETCH_SNOWFLAKE_METADATA)
    return result.fetchall()


@reflection.cache
def _get_table_comment(self, connection, table_name, schema=None, **kw):
    """
    Returns comment of table.
    """
    sql_command = "select * FROM information_schema.tables WHERE TABLE_SCHEMA ILIKE '{}' and TABLE_NAME ILIKE '{}'".format(
        self.normalize_name(schema),
        table_name,
    )

    cursor = connection.execute(text(sql_command))
    return cursor.fetchone()  # pylint: disable=protected-access


@reflection.cache
def get_unique_constraints(self, connection, table_name, schema=None, **kw):
    return []


def normalize_names(self, name):
    return name


SnowflakeDialect.get_table_names = get_table_names
SnowflakeDialect.normalize_name = normalize_names
SnowflakeDialect._get_table_comment = _get_table_comment
SnowflakeDialect.get_unique_constraints = get_unique_constraints
