from typing import Any, Dict, List, Optional

import re
import os
import copy
import json
import logging
from pathlib import Path

import yaml
import networkx as nx
from jsonschema.validators import Draft7Validator
from jsonschema.exceptions import ValidationError

from haystack import __version__
from haystack.nodes.base import BaseComponent, RootNode
from haystack.nodes._json_schema import inject_definition_in_schema, JSON_SCHEMAS_PATH
from haystack.errors import PipelineError, PipelineConfigError, PipelineSchemaError


logger = logging.getLogger(__name__)


VALID_INPUT_REGEX = re.compile(r"^[-a-zA-Z0-9_/\\.:]+$")
VALID_ROOT_NODES = ["Query", "File"]


def get_pipeline_definition(pipeline_config: Dict[str, Any], pipeline_name: Optional[str] = None) -> Dict[str, Any]:
    """
    Get the definition of Pipeline from a given pipeline config. If the config contains more than one Pipeline,
    then the pipeline_name must be supplied.

    :param pipeline_config: Dict Pipeline config parsed as a dictionary.
    :param pipeline_name: name of the Pipeline.
    """
    if pipeline_name is None:
        if len(pipeline_config["pipelines"]) != 1:
            raise PipelineConfigError("The YAML contains multiple pipelines. Please specify the pipeline name to load.")
        return pipeline_config["pipelines"][0]

    matching_pipelines = [p for p in pipeline_config["pipelines"] if p["name"] == pipeline_name]

    if len(matching_pipelines) == 1:
        return matching_pipelines[0]

    if not matching_pipelines:
        raise PipelineConfigError(
            f"Cannot find any pipeline with name '{pipeline_name}' declared in the YAML file. "
            f"Existing pipelines: {[p['name'] for p in pipeline_config['pipelines']]}"
        )
    raise PipelineConfigError(
        f"There's more than one pipeline called '{pipeline_name}' in the YAML file. "
        "Please give the two pipelines different names."
    )


def get_component_definitions(pipeline_config: Dict[str, Any], overwrite_with_env_variables: bool = True) -> Dict[str, Any]:
    """
    Returns the definitions of all components from a given pipeline config.

    :param pipeline_config: Dict Pipeline config parsed as a dictionary.
    :param overwrite_with_env_variables: Overwrite the YAML configuration with environment variables. For example,
                                         to change index name param for an ElasticsearchDocumentStore, an env
                                         variable 'MYDOCSTORE_PARAMS_INDEX=documents-2021' can be set. Note that an
                                         `_` sign must be used to specify nested hierarchical properties.
    """
    component_definitions = {}  # definitions of each component from the YAML.
    raw_component_definitions = copy.deepcopy(pipeline_config["components"])
    for component_definition in raw_component_definitions:
        if overwrite_with_env_variables:
            _overwrite_with_env_variables(component_definition)
        name = component_definition.pop("name")
        component_definitions[name] = component_definition

    return component_definitions


def read_pipeline_config_from_yaml(path: Path) -> Dict[str, Any]:
    """
    Parses YAML files into Python objects.
    Fails if the file does not exist.
    """
    if not os.path.isfile(path):
        raise FileNotFoundError(f"Not found: {path}")
    with open(path, "r", encoding="utf-8") as stream:
        return yaml.safe_load(stream)


def validate_config_strings(pipeline_config: Any):
    """
    Ensures that strings used in the pipelines configuration
    contain only alphanumeric characters and basic punctuation.
    """
    try:
        if isinstance(pipeline_config, dict):
            for key, value in pipeline_config.items():
                validate_config_strings(key)
                validate_config_strings(value)

        elif isinstance(pipeline_config, list):
            for value in pipeline_config:
                validate_config_strings(value)

        else:
            if not VALID_INPUT_REGEX.match(str(pipeline_config)):
                raise PipelineConfigError(
                    f"'{pipeline_config}' is not a valid variable name or value. "
                    "Use alphanumeric characters or dash, underscore and colon only."
                )
    except RecursionError as e:
        raise PipelineConfigError("The given pipeline configuration is recursive, can't validate it.") from e


def build_component_dependency_graph(
    pipeline_definition: Dict[str, Any], component_definitions: Dict[str, Any]
) -> nx.DiGraph:
    """
    Builds a dependency graph between components. Dependencies are:
    - referenced components during component build time (e.g. init params)
    - predecessor components in the pipeline that produce the needed input

    This enables sorting the components in a working and meaningful order for instantiation using topological sorting.

    :param pipeline_definition: the definition of the pipeline (e.g. use get_pipeline_definition() to obtain it)
    :param component_definitions: the definition of the pipeline components (e.g. use get_component_definitions() to obtain it)
    """
    graph = nx.DiGraph()
    for component_name, component_definition in component_definitions.items():
        params = component_definition.get("params", {})
        referenced_components: List[str] = list()
        for param_value in params.values():
            # Currently we don't do any additional type validation here.
            # See https://github.com/deepset-ai/haystack/pull/2253#discussion_r815951591.
            if param_value in component_definitions:
                referenced_components.append(param_value)
        for referenced_component in referenced_components:
            graph.add_edge(referenced_component, component_name)
    for node in pipeline_definition["nodes"]:
        node_name = node["name"]
        graph.add_node(node_name)
        for input in node["inputs"]:
            if input in component_definitions:
                # Special case for (actually permitted) cyclic dependencies between two components:
                # e.g. DensePassageRetriever depends on ElasticsearchDocumentStore.
                # In indexing pipelines ElasticsearchDocumentStore depends on DensePassageRetriever's output.
                # But this second dependency is looser, so we neglect it.
                if not graph.has_edge(node_name, input):
                    graph.add_edge(input, node_name)
    return graph


def validate_yaml(path: Path, strict_version_check: bool = False, overwrite_with_env_variables: bool = True):
    """
    Ensures that the given YAML file can be loaded without issues.

    Validates:
    - The YAML schema, so the configuration's structure and types
    - The pipeline's graph, so that all nodes are connected properly

    Does not validate:
    - The content of each node's parameter (except for their type),
      as this method does NOT load the nodes during the validation.

    :param path: path to the YAML file to validatethe configuration to validate
    :param strict_version_check: whether to fail in case of a version mismatch (throws a warning otherwise)
    :param overwrite_with_env_variables: Overwrite the YAML configuration with environment variables. For example,
                                         to change index name param for an ElasticsearchDocumentStore, an env
                                         variable 'MYDOCSTORE_PARAMS_INDEX=documents-2021' can be set. Note that an
                                         `_` sign must be used to specify nested hierarchical properties.
    :return: None if validation is successful
    :raise: `PipelineConfigError` in case of issues.
    """
    pipeline_config = read_pipeline_config_from_yaml(path)
    validate_config(config=pipeline_config, strict_version_check=strict_version_check)
    logging.debug(f"'{path}' contains valid Haystack pipelines.")


def validate_config(
    pipeline_config: Dict[str, Any], strict_version_check: bool = False, overwrite_with_env_variables: bool = True
):
    """
    Ensures that the given YAML file can be loaded without issues.

    Validates:
    - The YAML schema, so the configuration's structure and types
    - The pipeline's graph, so that all nodes are connected properly

    Does not validate:
    - The content of each node's parameter (except for their type),
      as this method does NOT load the nodes during the validation.

    :param pipeline_config: the configuration to validate (from reading up a YAML file or from .get_config())
    :param strict_version_check: whether to fail in case of a version mismatch (throws a warning otherwise)
    :param overwrite_with_env_variables: Overwrite the YAML configuration with environment variables. For example,
                                         to change index name param for an ElasticsearchDocumentStore, an env
                                         variable 'MYDOCSTORE_PARAMS_INDEX=documents-2021' can be set. Note that an
                                         `_` sign must be used to specify nested hierarchical properties.
    :return: None if validation is successful
    :raise: `PipelineConfigError` in case of issues.
    """
    validate_schema(config=pipeline_config, strict_version_check=strict_version_check)

    for pipeline_definition in pipeline_config["pipelines"]:
        component_definitions = get_component_definitions(
            pipeline_config=pipeline_config, overwrite_with_env_variables=overwrite_with_env_variables
        )
        validate_pipeline_graph(pipeline_definition=pipeline_definition, component_definitions=component_definitions)


def validate_schema(pipeline_config: Dict, strict_version_check: bool = False) -> None:
    """
    Check that the YAML abides the JSON schema, so that every block
    of the pipeline configuration file contains all required information
    and that every node's type and parameter are correct.

    Does NOT validate the pipeline's graph, nor the values given to
    the node's parameters (apart from their type).

    :param pipeline_config: the configuration to validate
    :param strict_version_check: whether to fail in case of a version mismatch (throws a warning otherwise)
    :return: None if validation is successful
    :raise: `PipelineConfigError` in case of issues.
    """
    validate_config_strings(pipeline_config)

    # Check for the version manually (to avoid validation errors)
    pipeline_version = pipeline_config.get("version", None)

    if pipeline_version != __version__:
        if strict_version_check:
            raise PipelineConfigError(
                f"Cannot load pipeline configuration of version {pipeline_version} "
                f"in Haystack version {__version__}\n"
                "Please check out the release notes (https://github.com/deepset-ai/haystack/releases/latest), "
                "the documentation (https://haystack.deepset.ai/components/pipelines#yaml-file-definitions) "
                "and fix your configuration accordingly."
            )
        ok_to_ignore_version = pipeline_version == "ignore" and "rc" in __version__
        if not ok_to_ignore_version:
            logging.warning(
                f"This pipeline is version '{pipeline_version}', but you're using Haystack {__version__}\n"
                "This might cause bugs and unexpected behaviors."
                "Please check out the release notes (https://github.com/deepset-ai/haystack/releases/latest), "
                "the documentation (https://haystack.deepset.ai/components/pipelines#yaml-file-definitions) "
                "and fix your configuration accordingly."
            )

    with open(JSON_SCHEMAS_PATH / f"haystack-pipeline-master.schema.json", "r") as schema_file:
        schema = json.load(schema_file)

    # Remove the version value from the schema to prevent validation errors on it - a version only have to be present.
    del schema["properties"]["version"]["const"]

    loaded_custom_nodes = []
    while True:
        try:
            Draft7Validator(schema).validate(instance=pipeline_config)
            break

        except ValidationError as validation:

            # If the validation comes from an unknown node, try to find it and retry:
            if list(validation.relative_schema_path) == ["properties", "components", "items", "anyOf"]:
                if validation.instance["type"] not in loaded_custom_nodes:

                    logger.info(
                        f"Missing definition for node of type {validation.instance['type']}. Looking into local classes..."
                    )
                    missing_component_class = BaseComponent.get_subclass(validation.instance["type"])
                    schema = inject_definition_in_schema(node_class=missing_component_class, schema=schema)
                    loaded_custom_nodes.append(validation.instance["type"])
                    continue

                # A node with the given name was in the schema, but something else is wrong with it.
                # Probably it references unknown classes in its init parameters.
                raise PipelineSchemaError(
                    f"Node of type {validation.instance['type']} found, but it failed validation. Possible causes:\n"
                    " - The node is missing some mandatory parameter\n"
                    " - Wrong indentation of some parameter in YAML\n"
                    "See the stacktrace for more information."
                ) from validation

            # Format the error to make it as clear as possible
            error_path = [
                i
                for i in list(validation.relative_schema_path)[:-1]
                if repr(i) != "'items'" and repr(i) != "'properties'"
            ]
            error_location = "->".join(repr(index) for index in error_path)
            if error_location:
                error_location = f"The error is in {error_location}."

            raise PipelineConfigError(
                f"Validation failed. {validation.message}. {error_location} " "See the stacktrace for more information."
            ) from validation

    logging.debug(f"The given configuration is valid according to the JSON schema.")


def validate_pipeline_graph(pipeline_definition: Dict[str, Any], component_definitions: Dict[str, Any]):
    """
    Validates a pipeline's graph without loading the nodes.

    :param pipeline_definition: from get_pipeline_definition()
    :param component_definitions: from get_component_definitions()
    """
    graph = nx.DiGraph()
    root_node_name = None
    for node in pipeline_definition["nodes"]:
        graph, root_node_name = _add_node_to_pipeline_graph(
            graph=graph, root_node_name=root_node_name, node=node, components=component_definitions
        )
    logging.debug(f"The graph for pipeline '{pipeline_definition['name']}' is valid.")


def _add_node_to_pipeline_graph(
    graph: nx.DiGraph,
    root_node_name: Optional[str],
    components: Dict[str, Dict[str, str]],
    node: Dict[str, Any],
    instance: BaseComponent = None,
):
    """
    Adds a single node to the provided graph, performing all necessary validation steps.

    :param graph: the graph to add the node to
    :param components: the whole list from get_component_definitions()
    :param node: `{"name": node_name, "inputs": [node_inputs]}` (the entry to add from get_component_definitions())
    :param instance: Optional instance of the node. Note that the instance is optional because in some cases
                     we want to be able to validate the graph without loading the nodes in the process.
    """
    # Validate node definition
    if not instance:
        _get_class_for_valid_node(node_name=node["name"], components=components)

    # If the graph is empty, let's first add a root node
    if len(graph) == 0:
        if root_node_name:
            raise PipelineConfigError(
                f"The root node name was given ({root_node_name}), but the graph is still empty. "
                "Please pass None as the root node name for an empty graph."
            )

        if not len(node["inputs"]) == 1:
            raise PipelineConfigError(
                f"The '{node['name']}' node is the first of the pipeline, so it can only take "
                f"one root node as input ([{'] or ['.join(VALID_ROOT_NODES)}], not {node['inputs']})."
            )
        root_node_name = node["inputs"][0]
        root_node = RootNode()
        root_node.name = root_node_name
        graph.add_node(root_node_name, inputs=[], component=root_node)

    if root_node_name not in VALID_ROOT_NODES:
        raise PipelineConfigError(f"Root node '{root_node_name}' is invalid. Available options are {VALID_ROOT_NODES}.")

    if instance is not None and not isinstance(instance, BaseComponent):
        raise PipelineError(
            f"The object provided for node {node['name']} is not a subclass of BaseComponent. "
            "Cannot add it to the pipeline."
        )

    if node["name"] in VALID_ROOT_NODES:
        raise PipelineConfigError(
            f"non root nodes cannot be named {' or '.join(VALID_ROOT_NODES)}. Choose another name."
        )
    
    # Check if the same instance has already been added to the graph before
    if instance:
        existing_node_names = [name for name, data in graph.nodes.items() if data["component"] == instance]
        if len(existing_node_names) > 0:
            raise PipelineConfigError(
                f"Cannot add node '{node['name']}' You have already added the same instance to the pipeline "
                f"under the name '{existing_node_names[0]}'."
            )

    graph.add_node(node["name"], component=instance, inputs=node["inputs"])

    for input_node in node["inputs"]:

        # Separate node and edge name, if specified
        input_node_name, input_edge_name = input_node, None
        if "." in input_node:
            input_node_name, input_edge_name = input_node.split(".")

        if input_node == root_node_name:
            input_edge_name = "output_1"

        elif input_node in VALID_ROOT_NODES:
            raise PipelineConfigError(
                f"This pipeline seems to contain two root nodes. "
                f"You can only use one root node (nodes named {' or '.join(VALID_ROOT_NODES)} per pipeline."
            )

        else:
            # Validate node definition and edge name
            input_node_type = _get_class_for_valid_node(node_name=input_node_name, components=components)
            input_node_edges_count = input_node_type.outgoing_edges

            if not input_edge_name:
                if input_node_edges_count != 1:  # Edge was not specified, but input node has many outputs
                    raise PipelineConfigError(
                        f"Can't connect {input_node_name} to {node['name']}: "
                        f"{input_node_name} has {input_node_edges_count} outgoing edges. "
                        "Please specify the output edge explicitly (like 'filetype_classifier.output_2')."
                    )
                input_edge_name = "output_1"

            if not input_edge_name.startswith("output_"):
                raise PipelineConfigError(
                    f"'{input_edge_name}' is not a valid edge name. "
                    "It must start with 'output_' and must contain no dots."
                )

            requested_edge_name = input_edge_name.split("_")[1]

            try:
                requested_edge = int(requested_edge_name)
            except ValueError:
                raise PipelineConfigError(
                    f"You must specified a numbered edge, like filetype_classifier.output_2, not {input_node}"
                )

            if not requested_edge <= input_node_edges_count:
                raise PipelineConfigError(
                    f"Cannot connect '{node['name']}' to '{input_node}', as {input_node_name} has only "
                    f"{input_node_edges_count} outgoing edge(s)."
                )

        graph.add_edge(input_node_name, node["name"], label=input_edge_name)

        # Check if adding this edge created a loop in the pipeline graph
        if not nx.is_directed_acyclic_graph(graph):
            graph.remove_node(node["name"])
            raise PipelineConfigError(f"Cannot add '{node['name']}': it will create a loop in the pipeline.")

    return graph, root_node_name


def _get_class_for_valid_node(node_name: str, components: Dict[str, Dict[str, str]]):
    try:
        node_type = components[node_name]["type"]
    except KeyError as e:
        raise PipelineConfigError(
            f"Cannot find node '{node_name}'. Make sure that a node "
            f"called '{node_name}' is defined under components."
        ) from e

    try:
        node_class = BaseComponent.get_subclass(node_type)
    except KeyError as e:
        raise PipelineConfigError(
            f"Node of type '{node_type}' not recognized. Check for typos in the node type."
        ) from e

    return node_class


def _overwrite_with_env_variables(component_definition: Dict[str, Any]):
    """
    Overwrite the pipeline config with environment variables. For example, to change index name param for an
    ElasticsearchDocumentStore, an env variable 'MYDOCSTORE_PARAMS_INDEX=documents-2021' can be set. Note that an
    `_` sign must be used to specify nested hierarchical properties.

    :param definition: a dictionary containing the YAML definition of a component.
    """
    env_prefix = f"{component_definition['name']}_params_".upper()
    for key, value in os.environ.items():
        if key.startswith(env_prefix):
            param_name = key.replace(env_prefix, "").lower()
            component_definition["params"][param_name] = value
            logger.info(
                f"Param '{param_name}' of component '{component_definition['name']}' overwritten with environment variable '{key}' value '{value}'."
            )
