from __future__ import annotations
import math
import importlib
import logging
import re
import os
import json
from typing import Dict, List, Optional

import cimgraph.queries.ontotext as ontotext
from cimgraph.databases import ConnectionInterface, ConnectionParameters, QueryResponse

from rdflib import Graph, Namespace, URIRef
from SPARQLWrapper import JSON, POST, SPARQLWrapper

_log = logging.getLogger(__name__)


class GraphDBConnection(ConnectionInterface):

    def __init__(self, connection_params: ConnectionParameters):
        self.cim_profile = connection_params.cim_profile
        # self.legacy_sparql = importlib.import_module('cimgraph.queries.sparql.' + self.cim_profile)
        self.cim = importlib.import_module('cimgraph.data_profile.' + self.cim_profile)
        self.namespace = connection_params.namespace
        self.iec61970_301 = connection_params.iec61970_301
        self.sparql_obj: Optional[SPARQLWrapper] = None
        self.connection_parameters = connection_params

        try:
            self.data_profile = Graph(store = 'Oxigraph')
            path = os.path.dirname(self.cim.__file__)
            self.data_profile.parse(f'{path}/{self.cim_profile}.rdfs',format='xml')
            self.reverse = URIRef('http://iec.ch/TC57/1999/rdf-schema-extensions-19990926#inverseRoleName')
        except:
            _log.warning='No RDFS schema found, reverting to default logic'
            self.data_profile = None

    def connect(self):
        if not self.sparql_obj:
            self.sparql_obj = SPARQLWrapper(self.connection_parameters.url)
            self.sparql_obj.setReturnFormat(JSON)

    def disconnect(self):
        self.sparql_obj = None

    def execute(self, query_message: str) -> QueryResponse:
        self.connect()
        self.sparql_obj.setQuery(query_message)
        self.sparql_obj.setMethod(POST)
        query_output = self.sparql_obj.query().convert()
        return query_output

    def create_new_graph(self, container: object) -> dict[type, dict[str, object]]:
        graph = {}
        # Get all nodes, terminal, and equipment by
        sparql_message = ontotext.get_all_nodes_ontotext(container, self.namespace)

        query_output = self.execute(sparql_message)

        for result in query_output['results']['bindings']:
            # Parse query results
            node_mrid = result['ConnectivityNode']['value']
            term_mrid = result['Terminal']['value']
            eq = json.loads(result['Equipment']['value'])
            eq_id = eq['@id']
            eq_class = eq['@type']
            # Add each object to graph
            node = self.create_object(graph, self.cim.ConnectivityNode, node_mrid)
            terminal = self.create_object(graph, self.cim.Terminal, term_mrid)
            if eq_class in self.cim.__all__:
                eq_class = eval(f'self.cim.{eq_class}')
                equipment = self.create_object(graph, eq_class, eq_id)

            else:
                _log.warning('object class missing from data profile:' + str(eq_class))
                continue
            # Link objects in graph
            if terminal not in equipment.Terminals:
                equipment.Terminals.append(terminal)
            if terminal not in node.Terminals:
                node.Terminals.append(terminal)
            setattr(terminal, 'ConnectivityNode', node)
            setattr(terminal, 'ConductingEquipment', equipment)

        return graph

    def get_edges_query(self, container: str | cim.ConnectivityNodeContainer,
                        graph: dict[type, dict[str, object]], cim_class: type):

        eq_mrids = list(graph[cim_class].keys())[0:100]
        sparql_message = ontotext.get_all_edges_ontotext(cim_class, eq_mrids, self.namespace,
                                                         self.iec61970_301)

        return sparql_message

    def get_all_edges(self, container: str | cim.ConnectivityNodeContainer,
                      graph: dict[type, dict[str, object]], cim_class: type):
        mrid_list = list(graph[cim_class].keys())
        num_nodes = len(mrid_list)
        for index in range(math.ceil(len(mrid_list) / 100)):
            eq_mrids = mrid_list[index * 100:(index + 1) * 100]
            #generate SPARQL message from correct loaders>sparql python script based on class name
            sparql_message = ontotext.get_all_edges_ontotext(cim_class, eq_mrids, self.namespace,
                                                             self.iec61970_301)
            #execute sparql query
            query_output = self.execute(sparql_message)
            self.edge_query_parser(query_output, container, graph, cim_class)

    def edge_query_parser(self, query_output, container: str | cim.ConnectivityNodeContainer,
                          graph: dict[type, dict[str, object]], cim_class: type):
        for result in query_output['results']['bindings']:
            if result['attribute']['value'] != 'type':    #skip 'type' and other single attributes

                is_association = False
                is_enumeration = False
                mRID = result['mRID']['value']    #get mRID
                attr = result['attribute']['value']    #edge attribute
                attribute = result['attribute']['value'].split('.')    #split edge attribute
                value = result['value']['value']    #get edge value

                if self.namespace in value:    #check if enumeration
                    enum_text = value.split(self.namespace)[1]
                    enum_text = enum_text.split('>')[0]
                    enum_class = enum_text.split('.')[0]
                    enum_value = enum_text.split('.')[1]
                    is_enumeration = True

                if 'edge' in result:    #check if association
                    is_association = True
                    edge = json.loads(result['edge']['value'])
                    edge_mRID = edge['@id']
                    edge_class = edge['@type']
                    if edge_class in self.cim.__all__:
                        edge_class = eval(f'self.cim.{edge_class}')
                    else:
                        print('unknown class', edge_class)
                        continue

                if is_association:    # if association to another CIM object

                    if attribute[1] in cim_class.__dataclass_fields__:    #check if forward attribute
                        self.create_edge(graph, cim_class, mRID, attribute[1], edge_class, edge_mRID)

                    elif self.data_profile is not None:    # use data profile to look up reverse attribute
                        attr_uri = URIRef(f'{self.namespace}{attr}')
                        reverse_uri = self.data_profile.value(object=attr_uri, predicate=self.reverse)
                        try:
                            reverse_attribute = reverse_uri.split('#')[1].split('.')[1]     # split string
                            self.create_edge(graph, cim_class, mRID, reverse_attribute, edge_class, edge_mRID)
                        except:
                            _log.warning(f'attribute {attr} missing from data profile')

                    else:    # fallback to use basic logic to identify
                        if attribute[0] in cim_class.__dataclass_fields__:    #check if first name is the attribute
                            self.create_edge(graph, cim_class, mRID, attribute[0], edge_class, edge_mRID)

                        elif attribute[0] + 's' in cim_class.__dataclass_fields__:    #check if attribute spelling is plural
                            self.create_edge(graph, cim_class, mRID, attribute[0] + 's', edge_class, edge_mRID)

                        elif attribute[1] + 's' in cim_class.__dataclass_fields__:    #check if attribute spelling is plural
                            self.create_edge(graph, cim_class, mRID, attribute[1] + 's', edge_class,edge_mRID)

                        elif edge_class.__name__ in cim_class.__dataclass_fields__:    #check if attribute spelling is plural
                            self.create_edge(graph, cim_class, mRID, edge_class.__name__, edge_class, edge_mRID)

                        elif edge_class.__name__ + 's' in cim_class.__dataclass_fields__:    #check if attribute spelling is plural
                            self.create_edge(graph, cim_class, mRID, edge_class.__name__ + 's', edge_class, edge_mRID)

                        else:    #fallback: match class type until a suitable parent edge class is found
                            parsed = False
                            for node_attr in list(cim_class.__dataclass_fields__.keys()):
                                attr_str = cim_class.__dataclass_fields__[node_attr].type
                                edge_parent = attr_str.split('[')[1].split(']')[0]
                                if edge_parent in self.cim.__all__:
                                    parent_class = eval(f'self.cim.{edge_parent}')
                                    if issubclass(edge_class, parent_class):
                                        self.create_edge(graph, cim_class, mRID, node_attr, edge_class, edge_mRID)
                                        parsed = True
                                        break
                            if not parsed:
                                _log.warning(f'unable to find match for {attr} for {mRID}')

                elif is_enumeration:
                    if enum_class in self.cim.__all__:    # if enumeration
                        edge_enum = eval(f'self.cim.{enum_class}(enum_value)')
                        setattr(graph[cim_class][mRID], attribute[1], edge_enum)
                else:
                    setattr(graph[cim_class][mRID], attribute[1], value)

    def create_edge(self, graph, cim_class, mRID, attribute, edge_class, edge_mRID):
        edge_object = self.create_object(graph, edge_class, edge_mRID)
        attribute_type = cim_class.__dataclass_fields__[attribute].type
        if 'List' in attribute_type:
            obj_list = getattr(graph[cim_class][mRID], attribute)
            if edge_object not in obj_list:
                obj_list.append(edge_object)
                setattr(graph[cim_class][mRID], attribute, obj_list)
        else:
            setattr(graph[cim_class][mRID], attribute, edge_object)

    def create_default_instances(self, feeder_mrid: str | cim.Feeder,
                                 mrid_list: List[str]) -> List[object]:
        """
        Creates empty CIM objects with the correct class type with mRID and name fields populated based on
        a list of mRID strings.
        Args:
            feeder_mrid (str | Feeder object): The mRID of the feeder or feeder object
            mrid_list (list[str]): A list of object mRID strings to be converted into CIM objects
        Returns:
            object_list: A list of CIM object instances
        """
        #generate correct sparql message using create_default.py
        sparql_message = self.legacy_sparql.get_class_type_sparql(feeder_mrid, mrid_list)
        #execute sparql query
        query_output = self.execute(sparql_message)
        # parse query results and add new CIM objects to list
        object_list = []
        for result in query_output['results']['bindings']:
            # print(result)
            cls = result['class']['value']
            mRID = result['mRID']['value']
            name = result['name']['value']
            try:
                object_list.append(eval(f"self.cim.{cls}(mRID='{mRID}', name = '{name}')"))
            except:
                _log.warning('object class missing from data profile:' + str(cls))
        return object_list

    def get_all_attributes(self, feeder_mrid: str | cim.Feeder,
                           graph: dict[type, dict[str, object]], cim_class: type):
        """ Populates all available attribute fields of CIM objects in the typed catalog of a specified CIM class.
        Objects are stored in memory, so no values are returned.
        Args:
        feeder_mrid (str | Feeder object): The mRID of the feeder or feeder object
        graph (dict[type, dict[str, object]]): The typed catalog of CIM objects organized by
            class type and object mRID
        cim_class (type): The CIM class type (e.g. cim:ACLineSegment)
        Returns:
        none
        """
        #generate SPARQL message from correct loaders>sparql python script based on class name
        sparql_message = self.get_attributes_query(feeder_mrid, graph, cim_class)
        #execute sparql query
        query_output = self.execute(sparql_message)

        for result in query_output['results']['bindings']:    #iterate through rows of response
            attribute_list = result.keys()
            mRID = result['mRID']['value']
            for attribute in attribute_list:
                try:    #check if attribute is in data profile
                    attribute_type = cim_class.__dataclass_fields__[attribute].type
                except:
                    #replace with warning message
                    _log.warning('attribute ' + str(attribute) + ' missing from ' +
                                 str(cim_class.__name__))

                if 'List' in attribute_type:    #check if attribute is association to a list of class objects
                    if '\'' in attribute_type:    #handling inconsistent '' marks in data profile
                        at_cls = re.match(r'List\[\'(.*)\']', attribute_type)
                        attribute_class = at_cls.group(1)
                    else:
                        at_cls = re.match(r'List\[(.*)]', attribute_type)
                        attribute_class = at_cls.group(1)
                    # pass query response of associated objects to list parser
                    self.query_list_parser(feeder_mrid, graph, cim_class, mRID, result, attribute,
                                           attribute_class, ';')
                elif 'Optional' in attribute_type:    #check if attribute is association to a class object
                    if '\'' in attribute_type:    #handling inconsistent '' marks in data profile
                        at_cls = re.match(r'Optional\[\'(.*)\']', attribute_type)
                        attribute_class = at_cls.group(1)
                    else:
                        at_cls = re.match(r'Optional\[(.*)]', attribute_type)
                        attribute_class = at_cls.group(1)

                    # pass query response of associated objects to list parser
                    self.query_parser(feeder_mrid, graph, cim_class, mRID, result, attribute,
                                      attribute_class, ';')
                else:    #otherwise assign query response

                    self.query_parser(feeder_mrid, graph, cim_class, mRID, result, attribute,
                                      attribute_class, ';')

    def get_attributes_query(self, feeder_mrid: str | cim.Feeder,
                             graph: dict[type, dict[str, object]], cim_class: type):
        """ Generates SPARQL edges query for a given catalog of objects and feeder id
        Args:
            feeder_mrid (str | Feeder object): The mRID of the feeder or feeder object
            graph (dict[type, dict[str, object]]): The typed catalog of CIM objects organized by
                class type and object mRID
            cim_class (type): The CIM class type (e.g. cim:ACLineSegment)
        Returns:
            sparql_message: query string that can be used in blazegraph connection or STOMP client
        none
        """
        sparql_func = getattr(self.legacy_sparql, f'{cim_class.__name__}SPARQL')
        sparql_message = sparql_func.get_all_attributes(feeder_mrid, graph)

        return sparql_message

    def query_parser(self, feeder_mrid, graph: Dict, class_name: str, mRID: str, query: List,
                     attribute: str, attribute_class: str, separator: str) -> object | str:
        value = query[attribute]['value']
        #if attribute is CIM class, then build CIM objects. otherwise assign to obj_list
        if attribute_class in self.cim.__all__:
            value = value.split(',')
            obj_mrid = value[0]
            try:
                obj_class = value[1]
            except:
                obj_class = attribute_class
            class_type = eval(f'self.cim.{obj_class}')
            if type(class_type) is type and len(obj_mrid) > 0:
                result = self.create_object(graph, class_type, obj_mrid)

            else:
                result = value

        else:
            result = value
        setattr(graph[class_name][mRID], attribute, result)

    def query_list_parser(self, feeder_mrid, graph: Dict, class_name: type, mRID: str, query: List,
                          attribute: str, attribute_class: str, separator: str):
        value = query[attribute]['value']
        values = value.split(separator)
        obj_list = []
        #if attribute is CIM class, then build CIM objects. otherwise assign to obj_list
        if attribute_class in self.cim.__all__:
            for value in values:
                value = value.split(',')
                obj_mrid = value[0]
                try:
                    obj_class = value[1]
                except:
                    obj_class = attribute_class
                class_type = eval(f'self.cim.{obj_class}')
                if type(class_type) is type and len(obj_mrid) > 0:
                    result = self.create_object(graph, class_type, obj_mrid)
                    obj_list.append(result)

        else:
            obj_list = values
        #set attribute of queried object to list parsed from query results
        setattr(graph[class_name][mRID], attribute, obj_list)

    def create_object(self, graph, class_type, mRID):

        if class_type not in graph.keys():
            graph[class_type] = {}

        if mRID in graph[class_type].keys():
            obj = graph[class_type][mRID]
        else:
            obj = class_type()
            setattr(obj, 'mRID', mRID)
            graph[class_type][mRID] = obj

        return obj

    def upload(self, graph):
        url = 'http://localhost:8889/bigdata/namespace/kb/sparql'

        prefix = """PREFIX r: <http://www.w3.org/1999/02/22-rdf-syntax-ns#>
        PREFIX cim: <http://iec.ch/TC57/CIM100#>
        PREFIX xsd: <http://www.w3.org/2001/XMLSchema#>
        """
        triples = []
        for cim_class in list(graph.keys()):

            for obj in graph[cim_class].values():
                #                 obj_triple = "<{url}#_{mRID}> a cim:{class_type}."
                obj_triple = """
        <urn:uuid:{mRID}> a cim:{class_type}.
                """
                triple = obj_triple.format(url=url, mRID=obj.mRID, class_type=cim_class.__name__)
                triples.append(triple)
                parent_classes = list(cim_class.__mro__)
                parent_classes.pop(len(parent_classes) - 1)
                for class_type in parent_classes:
                    attribute_list = list(class_type.__annotations__.keys())
                    for attribute in attribute_list:

                        try:    #check if attribute is in data profile
                            attribute_type = cim_class.__dataclass_fields__[attribute].type
                        except:
                            #replace with warning message
                            _log.warning('attribute ' + str(attribute) + ' missing from ' +
                                         str(cim_class.__name__))

                        if 'List' not in attribute_type:    #check if attribute is association to a class object
                            if '\'' in attribute_type:    #handling inconsistent '' marks in data profile
                                at_cls = re.match(r'Optional\[\'(.*)\']', attribute_type)
                                attribute_class = at_cls.group(1)
                            else:
                                at_cls = re.match(r'Optional\[(.*)]', attribute_type)
                                attribute_class = at_cls.group(1)

                            if attribute_class in self.cim.__all__:
                                attr_obj = getattr(obj, attribute)
                                if attr_obj is not None:
                                    value = attr_obj.mRID
                                    attr = """
        <urn:uuid:{mRID}> cim:{class_type}.{att} <urn:uuid:{value}>.
                                    """

                                    triple = attr.format(url=url,
                                                         mRID=obj.mRID,
                                                         class_type=class_type.__name__,
                                                         att=attribute,
                                                         value=value)
                                    triples.append(triple)

                            else:
                                value = item_dump(getattr(obj, attribute))
                                if value:
                                    #                              <{url}#_{mRID}> cim:{class_type}.{attr} \"{value}\".
                                    attr = """
        <urn:uuid:{mRID}> cim:{class_type}.{attr} \"{value}\".
                                     """
                                    triple = attr.format(url=url,
                                                         mRID=obj.mRID,
                                                         class_type=class_type.__name__,
                                                         attr=attribute,
                                                         value=value)
                                    triples.append(triple)
        triples.append('}')
        query = prefix + ' INSERT DATA { ' + ''.join(triples)

        self.execute(query)
        return query
