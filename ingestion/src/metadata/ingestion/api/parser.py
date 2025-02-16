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

"""
Helper to parse workflow configurations
"""
from typing import Optional, Type, TypeVar, Union

from pydantic import BaseModel, ValidationError

from metadata.generated.schema.entity.services.dashboardService import (
    DashboardConnection,
    DashboardServiceType,
)
from metadata.generated.schema.entity.services.databaseService import (
    DatabaseConnection,
    DatabaseServiceType,
)
from metadata.generated.schema.entity.services.messagingService import (
    MessagingConnection,
    MessagingServiceType,
)
from metadata.generated.schema.entity.services.metadataService import (
    MetadataConnection,
    MetadataServiceType,
)
from metadata.generated.schema.metadataIngestion.workflow import (
    OpenMetadataWorkflowConfig,
)
from metadata.utils.logger import ingestion_logger

logger = ingestion_logger()

T = TypeVar("T", bound=BaseModel)


def get_service_type(
    source_type: str,
) -> Union[
    Type[DashboardConnection],
    Type[DatabaseConnection],
    Type[MessagingConnection],
    Type[MetadataConnection],
]:
    """
    Return the service type for a source string
    :param source_type: source string
    :return: service connection type
    """
    if source_type in DatabaseServiceType.__members__:
        return DatabaseConnection
    if source_type in DashboardServiceType.__members__:
        return DashboardConnection
    if source_type in MessagingServiceType.__members__:
        return MessagingConnection
    if source_type in MetadataServiceType.__members__:
        return MetadataConnection

    raise ValueError(f"Cannot find the service type of {source_type}")


def get_connection_class(
    source_type: str,
    service_type: Union[
        Type[DashboardConnection],
        Type[DatabaseConnection],
        Type[MessagingConnection],
        Type[MetadataConnection],
    ],
) -> T:
    """
    Build the connection class path, import and return it
    :param source_type: e.g., Glue
    :param service_type: e.g., DatabaseConnection
    :return: e.g., GlueConnection
    """

    # Get all the module path minus the file.
    # From metadata.generated.schema.entity.services.databaseService we get metadata.generated.schema.entity.services
    module_path = ".".join(service_type.__module__.split(".")[:-1])
    connection_path = service_type.__name__.lower().replace("connection", "")
    connection_module = source_type[0].lower() + source_type[1:] + "Connection"

    class_name = source_type + "Connection"
    class_path = f"{module_path}.connections.{connection_path}.{connection_module}"

    connection_class = getattr(
        __import__(class_path, globals(), locals(), [class_name]), class_name
    )

    return connection_class


def parse_workflow_config_gracefully(
    config_dict: dict,
) -> Optional[OpenMetadataWorkflowConfig]:
    """
    This function either correctly parses the pydantic class, or
    throws a scoped error while fetching the required source connection
    class.

    :param config_dict: JSON workflow config
    :return:workflow config or scoped error
    """

    try:
        workflow_config = OpenMetadataWorkflowConfig.parse_obj(config_dict)

        return workflow_config

    except ValidationError:

        # Unsafe access to the keys. Allow a KeyError if the config is not well formatted
        source_type = config_dict["source"]["serviceConnection"]["config"]["type"]
        logger.error(
            f"Error parsing the Workflow Configuration for {source_type} ingestion"
        )

        service_type = get_service_type(source_type)
        connection_class = get_connection_class(source_type, service_type)

        # Parse the dictionary with the scoped class
        connection_class.parse_obj(config_dict["source"]["serviceConnection"]["config"])
