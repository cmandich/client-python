# coding: utf-8

import base64
import datetime
import json
import os
import uuid
from typing import Any, Dict, List, Optional, Union

import datefinder
import dateutil.parser
import pytz
from cachetools import LRUCache

from pycti.api import LOGGER as API_LOGGER
from pycti.entities.opencti_identity import Identity
from pycti.utils.constants import (
    IdentityTypes,
    LocationTypes,
    MultipleRefRelationship,
    StixCyberObservableTypes,
    ThreatActorTypes,
)
from pycti.utils.opencti_stix2_splitter import OpenCTIStix2Splitter
from pycti.utils.opencti_stix2_update import OpenCTIStix2Update
from pycti.utils.opencti_stix2_utils import (
    OBSERVABLES_VALUE_INT,
    STIX_CYBER_OBSERVABLE_MAPPING,
)

datefinder.ValueError = ValueError, OverflowError
utc = pytz.UTC

# Spec version
SPEC_VERSION = "2.1"

# Extensions
STIX_EXT_OCTI = "extension-definition--ea279b3e-5c71-4632-ac08-831c66a786ba"
STIX_EXT_OCTI_SCO = "extension-definition--f93e2c80-4231-4f9a-af8b-95c9bd566a82"
STIX_EXT_MITRE = "extension-definition--322b8f77-262a-4cb8-a915-1e441e00329b"


class OpenCTIStix2:
    """Python API for Stix2 in OpenCTI

    :param opencti: OpenCTI instance
    """

    def __init__(self, opencti):
        self.opencti = opencti
        self.stix2_update = OpenCTIStix2Update(opencti)
        self.mapping_cache = LRUCache(maxsize=50000)
        self.mapping_cache_permanent = {}

    ######### UTILS
    # region utils
    def unknown_type(self, stix_object: Dict) -> None:
        API_LOGGER.error(
            'Unknown object type "%s", doing nothing...', stix_object["type"]
        )

    def convert_markdown(self, text: str) -> str:
        """converts input text to markdown style code annotation

        :param text: input text
        :type text: str
        :return: sanitized text with markdown style code annotation
        :rtype: str
        """
        if text is not None:
            return text.replace("<code>", "`").replace("</code>", "`")
        else:
            return None

    def format_date(self, date: Any = None) -> str:
        """converts multiple input date formats to OpenCTI style dates

        :param date: input date
        :type date: Any [datetime, date, str or none]
        :return: OpenCTI style date
        :rtype: string
        """
        if isinstance(date, datetime.datetime):
            date_value = date
        elif isinstance(date, datetime.date):
            date_value = datetime.datetime.combine(date, datetime.datetime.min.time())
        elif isinstance(date, str):
            try:
                date_value = dateutil.parser.parse(date)
            except (dateutil.parser.ParserError, TypeError, OverflowError) as e:
                raise ValueError(f"{e}: {date} does not contain a valid date string")
        else:
            date_value = datetime.datetime.utcnow()

        if not date_value.tzinfo:
            API_LOGGER.info("No timezone found. Setting to UTC")
            date_value = date_value.replace(tzinfo=datetime.timezone.utc)

        return date_value.isoformat(timespec="milliseconds").replace("+00:00", "Z")

    def filter_objects(self, uuids: List, objects: List) -> List:
        """filters objects based on UUIDs

        :param uuids: list of UUIDs
        :type uuids: list
        :param objects: list of objects to filter
        :type objects: list
        :return: list of filtered objects
        :rtype: list
        """

        result = []
        if objects is not None:
            for item in objects:
                if "id" in item and item["id"] not in uuids:
                    result.append(item)
        return result

    def pick_aliases(self, stix_object: Dict) -> Optional[List]:
        """check stix2 object for multiple aliases and return a list

        :param stix_object: valid stix2 object
        :type stix_object:
        :return: list of aliases
        :rtype: list
        """

        # Add aliases
        if "x_opencti_aliases" in stix_object:
            return stix_object["x_opencti_aliases"]
        elif "x_mitre_aliases" in stix_object:
            return stix_object["x_mitre_aliases"]
        elif "x_amitt_aliases" in stix_object:
            return stix_object["x_amitt_aliases"]
        elif "aliases" in stix_object:
            return stix_object["aliases"]
        return None

    def check_max_marking_definition(
        self, max_marking_definition_entity: Dict, entity_marking_definitions: List
    ) -> bool:
        """checks if a list of marking definitions conforms with a given max level

        :param max_marking_definition_entity: the maximum allowed marking definition level
        :type max_marking_definition_entity: str, optional
        :param entity_marking_definitions: list of entities to check
        :type entity_marking_definitions: list
        :return: `True` if the list conforms with max marking definition
        :rtype: bool
        """

        # Max is not set, return True
        if max_marking_definition_entity is None:
            return True
        # Filter entity markings definition to the max_marking_definition type
        typed_entity_marking_definitions = []
        for entity_marking_definition in entity_marking_definitions:
            if (
                entity_marking_definition["definition_type"]
                == max_marking_definition_entity["definition_type"]
            ):
                typed_entity_marking_definitions.append(entity_marking_definition)
        # No entity marking defintions of the max_marking_definition type
        if len(typed_entity_marking_definitions) == 0:
            return True

        # Check if level is less or equal to max
        for typed_entity_marking_definition in typed_entity_marking_definitions:
            if (
                typed_entity_marking_definition["x_opencti_order"]
                <= max_marking_definition_entity["x_opencti_order"]
            ):
                return True
        return False

    def import_bundle_from_file(
        self, file_path: str, update: bool = False, types: List = None
    ) -> Optional[List]:
        """import a stix2 bundle from a file

        :param file_path: valid path to the file
        :type file_path: str
        :param update: whether to updated data in the database, defaults to False
        :type update: bool, optional
        :param types: list of stix2 types, defaults to None
        :type types: list, optional
        :return: list of imported stix2 objects
        :rtype: List
        """
        if not os.path.isfile(file_path):
            API_LOGGER.error("The bundle file does not exists")
            return None
        with open(os.path.join(file_path), encoding="utf-8") as file:
            data = json.load(file)
        return self.import_bundle(data, update, types)

    def import_bundle_from_json(
        self,
        json_data: Union[str, bytes],
        update: bool = False,
        types: List = None,
        retry_number: int = None,
    ) -> List:
        """import a stix2 bundle from JSON data

        :param json_data: JSON data
        :type json_data:
        :param update: whether to updated data in the database, defaults to False
        :type update: bool, optional
        :param types: list of stix2 types, defaults to None
        :type types: list, optional
        :return: list of imported stix2 objects
        :rtype: List
        """
        data = json.loads(json_data)
        return self.import_bundle(
            data,
            update,
            types,
            retry_number,
        )

    def resolve_author(self, title: str) -> Optional[Identity]:
        if "fireeye" in title.lower() or "mandiant" in title.lower():
            return self.get_author("FireEye")
        if "eset" in title.lower():
            return self.get_author("ESET")
        if "dragos" in title.lower():
            return self.get_author("Dragos")
        if "us-cert" in title.lower():
            return self.get_author("US-CERT")
        if (
            "unit 42" in title.lower()
            or "unit42" in title.lower()
            or "palo alto" in title.lower()
        ):
            return self.get_author("Palo Alto Networks")
        if "accenture" in title.lower():
            return self.get_author("Accenture")
        if "symantec" in title.lower():
            return self.get_author("Symantec")
        if "trendmicro" in title.lower() or "trend micro" in title.lower():
            return self.get_author("Trend Micro")
        if "mcafee" in title.lower():
            return self.get_author("McAfee")
        if "crowdstrike" in title.lower():
            return self.get_author("CrowdStrike")
        if "securelist" in title.lower() or "kaspersky" in title.lower():
            return self.get_author("Kaspersky")
        if "f-secure" in title.lower():
            return self.get_author("F-Secure")
        if "checkpoint" in title.lower():
            return self.get_author("CheckPoint")
        if "talos" in title.lower():
            return self.get_author("Cisco Talos")
        if "secureworks" in title.lower():
            return self.get_author("Dell SecureWorks")
        if "microsoft" in title.lower():
            return self.get_author("Microsoft")
        if "mitre att&ck" in title.lower():
            return self.get_author("The MITRE Corporation")
        return None

    def get_author(self, name: str) -> Identity:
        if name in self.mapping_cache:
            return self.mapping_cache[name]
        else:
            author = self.opencti.identity.create(
                type="Organization",
                name=name,
                description="",
            )
            self.mapping_cache[name] = author
            return author

    def extract_embedded_relationships(
        self, stix_object: Dict, types: List = None
    ) -> Dict:
        """extracts embedded relationship objects from a stix2 entity

        :param stix_object: valid stix2 object
        :type stix_object:
        :param types: list of stix2 types, defaults to None
        :type types: list, optional
        :return: embedded relationships as dict
        :rtype: dict
        """

        # Created By Ref
        created_by_id = None
        if "created_by_ref" in stix_object:
            created_by_id = stix_object["created_by_ref"]
        elif "x_opencti_created_by_ref" in stix_object:
            created_by_id = stix_object["x_opencti_created_by_ref"]
        elif (
            self.opencti.get_attribute_in_extension("created_by_ref", stix_object)
            is not None
        ):
            created_by_id = self.opencti.get_attribute_in_extension(
                "created_by_ref", stix_object
            )
        # Object Marking Refs
        object_marking_ids = (
            stix_object["object_marking_refs"]
            if "object_marking_refs" in stix_object
            else []
        )

        # Open vocabularies
        object_open_vocabularies = {}
        if self.mapping_cache_permanent.get("vocabularies_definition_fields") is None:
            self.mapping_cache_permanent["vocabularies_definition_fields"] = []
            query = """
                    query getVocabCategories {
                      vocabularyCategories {
                        key
                        fields{
                          key
                          required
                        }
                      }
                    }
                """
            result = self.opencti.query(query)
            for category in result["data"]["vocabularyCategories"]:
                for field in category["fields"]:
                    self.mapping_cache_permanent[
                        "vocabularies_definition_fields"
                    ].append(field)
                    self.mapping_cache_permanent["category_" + field["key"]] = category[
                        "key"
                    ]
        if any(
            field["key"] in stix_object
            for field in self.mapping_cache_permanent["vocabularies_definition_fields"]
        ):
            for f in self.mapping_cache_permanent["vocabularies_definition_fields"]:
                if stix_object.get(f["key"]) is None:
                    continue
                if isinstance(stix_object.get(f["key"]), list):
                    object_open_vocabularies[f["key"]] = []
                    for vocab in stix_object[f["key"]]:
                        object_open_vocabularies[f["key"]].append(
                            self.opencti.vocabulary.handle_vocab(
                                vocab, self.mapping_cache_permanent, field=f
                            )["name"]
                        )
                else:
                    object_open_vocabularies[
                        f["key"]
                    ] = self.opencti.vocabulary.handle_vocab(
                        stix_object[f["key"]], self.mapping_cache_permanent, field=f
                    )[
                        "name"
                    ]

        # Object Labels
        object_label_ids = []
        if (
            "labels" not in stix_object
            and self.opencti.get_attribute_in_extension("labels", stix_object)
            is not None
        ):
            stix_object["labels"] = self.opencti.get_attribute_in_extension(
                "labels", stix_object
            )
        if "labels" in stix_object:
            for label in stix_object["labels"]:
                if "label_" + label in self.mapping_cache:
                    label_data = self.mapping_cache["label_" + label]
                else:
                    # Fail in label creation is allowed
                    label_data = self.opencti.label.read_or_create_unchecked(
                        value=label
                    )
                if label_data is not None:
                    self.mapping_cache["label_" + label] = label_data
                    object_label_ids.append(label_data["id"])
        elif "x_opencti_labels" in stix_object:
            for label in stix_object["x_opencti_labels"]:
                if "label_" + label in self.mapping_cache:
                    label_data = self.mapping_cache["label_" + label]
                else:
                    # Fail in label creation is allowed
                    label_data = self.opencti.label.read_or_create_unchecked(
                        value=label
                    )
                if label_data is not None:
                    self.mapping_cache["label_" + label] = label_data
                    object_label_ids.append(label_data["id"])
        elif "x_opencti_tags" in stix_object:
            for tag in stix_object["x_opencti_tags"]:
                label = tag["value"]
                color = tag["color"] if "color" in tag else None
                if "label_" + label in self.mapping_cache:
                    label_data = self.mapping_cache["label_" + label]
                else:
                    # Fail in label creation is allowed
                    label_data = self.opencti.label.read_or_create_unchecked(
                        value=label, color=color
                    )
                if label_data is not None:
                    self.mapping_cache["label_" + label] = label_data
                    object_label_ids.append(label_data["id"])
        # Kill Chain Phases
        kill_chain_phases_ids = []
        if (
            "kill_chain_phases" not in stix_object
            and self.opencti.get_attribute_in_extension(
                "kill_chain_phases", stix_object
            )
            is not None
        ):
            stix_object["kill_chain_phases"] = self.opencti.get_attribute_in_extension(
                "kill_chain_phases", stix_object
            )
        if "kill_chain_phases" in stix_object:
            for kill_chain_phase in stix_object["kill_chain_phases"]:
                if (
                    kill_chain_phase["kill_chain_name"] + kill_chain_phase["phase_name"]
                    in self.mapping_cache
                ):
                    kill_chain_phase = self.mapping_cache[
                        kill_chain_phase["kill_chain_name"]
                        + kill_chain_phase["phase_name"]
                    ]
                else:
                    if (
                        "x_opencti_order" not in kill_chain_phase
                        and self.opencti.get_attribute_in_extension(
                            "order", kill_chain_phase
                        )
                        is not None
                    ):
                        kill_chain_phase[
                            "x_opencti_order"
                        ] = self.opencti.get_attribute_in_extension(
                            "order", kill_chain_phase
                        )
                    kill_chain_phase = self.opencti.kill_chain_phase.create(
                        kill_chain_name=kill_chain_phase["kill_chain_name"],
                        phase_name=kill_chain_phase["phase_name"],
                        x_opencti_order=kill_chain_phase["x_opencti_order"]
                        if "x_opencti_order" in kill_chain_phase
                        else 0,
                        stix_id=kill_chain_phase["id"]
                        if "id" in kill_chain_phase
                        else None,
                    )
                    self.mapping_cache[
                        kill_chain_phase["kill_chain_name"]
                        + kill_chain_phase["phase_name"]
                    ] = {
                        "id": kill_chain_phase["id"],
                        "type": kill_chain_phase["entity_type"],
                    }
                kill_chain_phases_ids.append(kill_chain_phase["id"])
        elif "x_opencti_kill_chain_phases" in stix_object:
            for kill_chain_phase in stix_object["x_opencti_kill_chain_phases"]:
                if (
                    kill_chain_phase["kill_chain_name"] + kill_chain_phase["phase_name"]
                    in self.mapping_cache
                ):
                    kill_chain_phase = self.mapping_cache[
                        kill_chain_phase["kill_chain_name"]
                        + kill_chain_phase["phase_name"]
                    ]
                else:
                    if (
                        "x_opencti_order" not in kill_chain_phase
                        and self.opencti.get_attribute_in_extension(
                            "order", kill_chain_phase
                        )
                        is not None
                    ):
                        kill_chain_phase[
                            "x_opencti_order"
                        ] = self.opencti.get_attribute_in_extension(
                            "order", kill_chain_phase
                        )
                    kill_chain_phase = self.opencti.kill_chain_phase.create(
                        kill_chain_name=kill_chain_phase["kill_chain_name"],
                        phase_name=kill_chain_phase["phase_name"],
                        x_opencti_order=kill_chain_phase["x_opencti_order"]
                        if "x_opencti_order" in kill_chain_phase
                        else 0,
                        stix_id=kill_chain_phase["id"]
                        if "id" in kill_chain_phase
                        else None,
                    )
                    self.mapping_cache[
                        kill_chain_phase["kill_chain_name"]
                        + kill_chain_phase["phase_name"]
                    ] = {
                        "id": kill_chain_phase["id"],
                        "type": kill_chain_phase["entity_type"],
                    }
                kill_chain_phases_ids.append(kill_chain_phase["id"])
        # Object refs
        object_refs_ids = (
            stix_object["object_refs"] if "object_refs" in stix_object else []
        )
        # External References
        reports = {}
        external_references_ids = []
        if (
            "external_references" not in stix_object
            and self.opencti.get_attribute_in_extension(
                "external_references", stix_object
            )
            is not None
        ):
            stix_object[
                "external_references"
            ] = self.opencti.get_attribute_in_extension(
                "external_references", stix_object
            )
        if "external_references" in stix_object:
            for external_reference in stix_object["external_references"]:
                try:
                    url = (
                        external_reference["url"]
                        if "url" in external_reference
                        else None
                    )
                    source_name = (
                        external_reference["source_name"]
                        if "source_name" in external_reference
                        else None
                    )
                    external_id = (
                        external_reference["external_id"]
                        if "external_id" in external_reference
                        else None
                    )
                    generated_ref_id = self.opencti.external_reference.generate_id(
                        url, source_name, external_id
                    )
                    if generated_ref_id is None:
                        continue
                    if generated_ref_id in self.mapping_cache:
                        external_reference_id = self.mapping_cache[generated_ref_id]
                    else:
                        external_reference_id = self.opencti.external_reference.create(
                            source_name=source_name,
                            url=url,
                            external_id=external_id,
                            description=external_reference["description"]
                            if "description" in external_reference
                            else None,
                        )["id"]
                    if "x_opencti_files" in external_reference:
                        for file in external_reference["x_opencti_files"]:
                            self.opencti.external_reference.add_file(
                                id=external_reference_id,
                                file_name=file["name"],
                                data=base64.b64decode(file["data"]),
                                mime_type=file["mime_type"],
                                no_trigger_import=file.get("no_trigger_import", False),
                            )
                    if (
                        self.opencti.get_attribute_in_extension(
                            "files", external_reference
                        )
                        is not None
                    ):
                        for file in self.opencti.get_attribute_in_extension(
                            "files", external_reference
                        ):
                            self.opencti.external_reference.add_file(
                                id=external_reference_id,
                                file_name=file["name"],
                                data=base64.b64decode(file["data"]),
                                mime_type=file["mime_type"],
                                no_trigger_import=file.get("no_trigger_import", False),
                            )
                    self.mapping_cache[generated_ref_id] = generated_ref_id
                    external_references_ids.append(external_reference_id)
                    if stix_object["type"] in [
                        "threat-actor",
                        "intrusion-set",
                        "campaign",
                        "incident",
                        "malware",
                        "relationship",
                    ] and (
                        types is not None and "external-reference-as-report" in types
                    ):
                        # Add a corresponding report
                        # Extract date
                        try:
                            if "description" in external_reference:
                                matches = datefinder.find_dates(
                                    external_reference["description"],
                                    base_date=datetime.datetime.fromtimestamp(0),
                                )
                            else:
                                matches = datefinder.find_dates(
                                    source_name,
                                    base_date=datetime.datetime.fromtimestamp(0),
                                )
                        except:
                            matches = None
                        published = None
                        yesterday = datetime.datetime.now() - datetime.timedelta(days=1)
                        default_date = datetime.datetime.fromtimestamp(1)
                        if matches is not None:
                            try:
                                for match in matches:
                                    if (
                                        match.timestamp() < yesterday.timestamp()
                                        and len(str(match.year)) == 4
                                    ):
                                        published = match.strftime("%Y-%m-%dT%H:%M:%SZ")
                                        break
                            except:
                                pass
                        if published is None:
                            published = default_date.strftime("%Y-%m-%dT%H:%M:%SZ")

                        if "mitre" in source_name and "name" in stix_object:
                            title = "[MITRE ATT&CK] " + stix_object["name"]
                            if "modified" in stix_object:
                                published = stix_object["modified"]
                        elif "amitt" in source_name and "name" in stix_object:
                            title = "[AM!TT] " + stix_object["name"]
                            if "modified" in stix_object:
                                published = stix_object["modified"]
                        else:
                            title = source_name

                        if "external_id" in external_reference:
                            title = (
                                title
                                + " ("
                                + str(external_reference["external_id"])
                                + ")"
                            )

                        if "marking_tlpclear" in self.mapping_cache:
                            object_marking_ref_result = self.mapping_cache[
                                "marking_tlpclear"
                            ]
                        else:
                            object_marking_ref_result = (
                                self.opencti.marking_definition.read(
                                    filters={
                                        "mode": "and",
                                        "filters": [
                                            {
                                                "key": "definition_type",
                                                "values": ["TLP"],
                                            },
                                            {
                                                "key": "definition",
                                                "values": ["TLP:CLEAR"],
                                            },
                                        ],
                                        "filterGroups": [],
                                    }
                                )
                            )
                            self.mapping_cache["marking_tlpclear"] = {
                                "id": object_marking_ref_result["id"]
                            }

                        author = self.resolve_author(title)
                        report = self.opencti.report.create(
                            name=title,
                            createdBy=author["id"] if author is not None else None,
                            objectMarking=[object_marking_ref_result["id"]],
                            externalReferences=[external_reference_id],
                            description=external_reference["description"]
                            if "description" in external_reference
                            else "",
                            report_types="threat-report",
                            published=published,
                            update=True,
                        )
                        reports[external_reference_id] = report
                except:
                    API_LOGGER.warn("Cannot generate external reference")
        elif "x_opencti_external_references" in stix_object:
            for external_reference in stix_object["x_opencti_external_references"]:
                url = external_reference["url"] if "url" in external_reference else None
                source_name = (
                    external_reference["source_name"]
                    if "source_name" in external_reference
                    else None
                )
                external_id = (
                    external_reference["external_id"]
                    if "external_id" in external_reference
                    else None
                )
                generated_ref_id = self.opencti.external_reference.generate_id(
                    url, source_name, external_id
                )
                if generated_ref_id is None:
                    continue
                if generated_ref_id in self.mapping_cache:
                    external_reference_id = self.mapping_cache[generated_ref_id]
                else:
                    external_reference_id = self.opencti.external_reference.create(
                        source_name=source_name,
                        url=url,
                        external_id=external_id,
                        description=external_reference["description"]
                        if "description" in external_reference
                        else None,
                    )["id"]
                if "x_opencti_files" in external_reference:
                    for file in external_reference["x_opencti_files"]:
                        self.opencti.external_reference.add_file(
                            id=external_reference_id,
                            file_name=file["name"],
                            data=base64.b64decode(file["data"]),
                            mime_type=file["mime_type"],
                            no_trigger_import=file.get("no_trigger_import", False),
                        )
                if (
                    self.opencti.get_attribute_in_extension("files", external_reference)
                    is not None
                ):
                    for file in self.opencti.get_attribute_in_extension(
                        "files", external_reference
                    ):
                        self.opencti.external_reference.add_file(
                            id=external_reference_id,
                            file_name=file["name"],
                            data=base64.b64decode(file["data"]),
                            mime_type=file["mime_type"],
                            no_trigger_import=file.get("no_trigger_import", False),
                        )
                self.mapping_cache[generated_ref_id] = generated_ref_id
                external_references_ids.append(external_reference_id)
        # Granted refs
        granted_refs_ids = []
        if (
            "granted_refs" not in stix_object
            and self.opencti.get_attribute_in_extension("granted_refs", stix_object)
            is not None
        ):
            granted_refs_ids = self.opencti.get_attribute_in_extension(
                "granted_refs", stix_object
            )

        return {
            "created_by": created_by_id,
            "object_marking": object_marking_ids,
            "object_label": object_label_ids,
            "open_vocabs": object_open_vocabularies,
            "kill_chain_phases": kill_chain_phases_ids,
            "object_refs": object_refs_ids,
            "granted_refs": granted_refs_ids,
            "external_references": external_references_ids,
            "reports": reports,
        }

    # endregion

    # region import
    def import_object(
        self, stix_object: Dict, update: bool = False, types: List = None
    ) -> Optional[List]:
        """import a stix2 object

        :param stix_object: valid stix2 object
        :type stix_object:
        :param update: whether to updated data in the database, defaults to False
        :type update: bool, optional
        :param types: list of stix2 types, defaults to None
        :type types: list, optional
        :return: list of imported stix2 objects
        :rtype: list
        """

        API_LOGGER.info(
            "Importing a %s (id: %s)", stix_object["type"], stix_object["id"]
        )

        # Extract
        embedded_relationships = self.extract_embedded_relationships(stix_object, types)
        created_by_id = embedded_relationships["created_by"]
        object_marking_ids = embedded_relationships["object_marking"]
        object_label_ids = embedded_relationships["object_label"]
        open_vocabs = embedded_relationships["open_vocabs"]
        kill_chain_phases_ids = embedded_relationships["kill_chain_phases"]
        object_refs_ids = embedded_relationships["object_refs"]
        external_references_ids = embedded_relationships["external_references"]
        reports = embedded_relationships["reports"]

        # Extra
        extras = {
            "created_by_id": created_by_id,
            "object_marking_ids": object_marking_ids,
            "object_label_ids": object_label_ids,
            "open_vocabs": open_vocabs,
            "kill_chain_phases_ids": kill_chain_phases_ids,
            "object_ids": object_refs_ids,
            "external_references_ids": external_references_ids,
            "reports": reports,
        }

        # Import
        importer = {
            "marking-definition": self.opencti.marking_definition.import_from_stix2,
            "attack-pattern": self.opencti.attack_pattern.import_from_stix2,
            "campaign": self.opencti.campaign.import_from_stix2,
            "channel": self.opencti.channel.import_from_stix2,
            "event": self.opencti.event.import_from_stix2,
            "note": self.opencti.note.import_from_stix2,
            "observed-data": self.opencti.observed_data.import_from_stix2,
            "opinion": self.opencti.opinion.import_from_stix2,
            "report": self.opencti.report.import_from_stix2,
            "grouping": self.opencti.grouping.import_from_stix2,
            "case-rfi": self.opencti.case_rfi.import_from_stix2,
            "x-opencti-case-rfi": self.opencti.case_rfi.import_from_stix2,
            "case-rft": self.opencti.case_rft.import_from_stix2,
            "x-opencti-case-rft": self.opencti.case_rft.import_from_stix2,
            "task": self.opencti.task.import_from_stix2,
            "x-opencti-task": self.opencti.task.import_from_stix2,
            "case-incident": self.opencti.case_incident.import_from_stix2,
            "x-opencti-case-incident": self.opencti.case_incident.import_from_stix2,
            "feedback": self.opencti.feedback.import_from_stix2,
            "x-opencti-feedback": self.opencti.feedback.import_from_stix2,
            "course-of-action": self.opencti.course_of_action.import_from_stix2,
            "data-component": self.opencti.data_component.import_from_stix2,
            "x-mitre-data-component": self.opencti.data_component.import_from_stix2,
            "data-source": self.opencti.data_source.import_from_stix2,
            "x-mitre-data-source": self.opencti.data_source.import_from_stix2,
            "identity": self.opencti.identity.import_from_stix2,
            "indicator": self.opencti.indicator.import_from_stix2,
            "infrastructure": self.opencti.infrastructure.import_from_stix2,
            "intrusion-set": self.opencti.intrusion_set.import_from_stix2,
            "location": self.opencti.location.import_from_stix2,
            "malware": self.opencti.malware.import_from_stix2,
            "malware-analysis": self.opencti.malware_analysis.import_from_stix2,
            "threat-actor": self.opencti.threat_actor.import_from_stix2,
            "tool": self.opencti.tool.import_from_stix2,
            "narrative": self.opencti.narrative.import_from_stix2,
            "vulnerability": self.opencti.vulnerability.import_from_stix2,
            "incident": self.opencti.incident.import_from_stix2,
        }
        do_import = importer.get(
            stix_object["type"],
            lambda **kwargs: self.unknown_type(stix_object),
        )
        stix_object_results = do_import(
            stixObject=stix_object, extras=extras, update=update
        )

        if stix_object_results is None:
            return None

        if not isinstance(stix_object_results, list):
            stix_object_results = [stix_object_results]

        for stix_object_result in stix_object_results:
            self.mapping_cache[stix_object["id"]] = {
                "id": stix_object_result["id"],
                "type": stix_object_result["entity_type"],
                "observables": stix_object_result["observables"]
                if "observables" in stix_object_result
                else [],
            }
            self.mapping_cache[stix_object_result["id"]] = {
                "id": stix_object_result["id"],
                "type": stix_object_result["entity_type"],
                "observables": stix_object_result["observables"]
                if "observables" in stix_object_result
                else [],
            }
            # Add reports from external references
            for external_reference_id in external_references_ids:
                if external_reference_id in reports:
                    self.opencti.report.add_stix_object_or_stix_relationship(
                        id=reports[external_reference_id]["id"],
                        stixObjectOrStixRelationshipId=stix_object_result["id"],
                    )
            # Add files
            if "x_opencti_files" in stix_object:
                for file in stix_object["x_opencti_files"]:
                    self.opencti.stix_domain_object.add_file(
                        id=stix_object_result["id"],
                        file_name=file["name"],
                        data=base64.b64decode(file["data"]),
                        mime_type=file["mime_type"],
                        no_trigger_import=file.get("no_trigger_import", False),
                    )
            if (
                self.opencti.get_attribute_in_extension("files", stix_object)
                is not None
            ):
                for file in self.opencti.get_attribute_in_extension(
                    "files", stix_object
                ):
                    self.opencti.stix_domain_object.add_file(
                        id=stix_object_result["id"],
                        file_name=file["name"],
                        data=base64.b64decode(file["data"]),
                        mime_type=file["mime_type"],
                        no_trigger_import=file.get("no_trigger_import", False),
                    )
        return stix_object_results

    def import_observable(
        self, stix_object: Dict, update: bool = False, types: List = None
    ) -> None:
        # Extract
        embedded_relationships = self.extract_embedded_relationships(stix_object, types)
        created_by_id = embedded_relationships["created_by"]
        object_marking_ids = embedded_relationships["object_marking"]
        object_label_ids = embedded_relationships["object_label"]
        open_vocabs = embedded_relationships["open_vocabs"]
        granted_refs_ids = embedded_relationships["granted_refs"]
        kill_chain_phases_ids = embedded_relationships["kill_chain_phases"]
        object_refs_ids = embedded_relationships["object_refs"]
        external_references_ids = embedded_relationships["external_references"]
        reports = embedded_relationships["reports"]

        # Extra
        extras = {
            "created_by_id": created_by_id,
            "object_marking_ids": object_marking_ids,
            "object_label_ids": object_label_ids,
            "open_vocabs": open_vocabs,
            "granted_refs_ids": granted_refs_ids,
            "kill_chain_phases_ids": kill_chain_phases_ids,
            "object_ids": object_refs_ids,
            "external_references_ids": external_references_ids,
            "reports": reports,
        }
        if stix_object["type"] == "simple-observable":
            stix_observable_result = self.opencti.stix_cyber_observable.create(
                simple_observable_id=stix_object["id"],
                simple_observable_key=stix_object["key"],
                simple_observable_value=stix_object["value"]
                if stix_object["key"] not in OBSERVABLES_VALUE_INT
                else int(stix_object["value"]),
                simple_observable_description=stix_object["description"]
                if "description" in stix_object
                else None,
                x_opencti_score=stix_object["x_opencti_score"]
                if "x_opencti_score" in stix_object
                else None,
                createdBy=extras["created_by_id"]
                if "created_by_id" in extras
                else None,
                objectMarking=extras["object_marking_ids"]
                if "object_marking_ids" in extras
                else [],
                objectLabel=extras["object_label_ids"]
                if "object_label_ids" in extras
                else None,
                externalReferences=extras["external_references_ids"]
                if "external_references_ids" in extras
                else None,
                createIndicator=stix_object["x_opencti_create_indicator"]
                if "x_opencti_create_indicator" in stix_object
                else None,
                objectOrganization=extras["granted_refs_ids"]
                if "granted_refs_ids" in extras
                else [],
                update=update,
            )
        else:
            stix_observable_result = self.opencti.stix_cyber_observable.create(
                observableData=stix_object,
                createdBy=extras["created_by_id"]
                if "created_by_id" in extras
                else None,
                objectMarking=extras["object_marking_ids"]
                if "object_marking_ids" in extras
                else [],
                objectLabel=extras["object_label_ids"]
                if "object_label_ids" in extras
                else None,
                externalReferences=extras["external_references_ids"]
                if "external_references_ids" in extras
                else None,
                objectOrganization=extras["granted_refs_ids"]
                if "granted_refs_ids" in extras
                else [],
                update=update,
            )
        if stix_observable_result is not None:
            # Add files
            if "x_opencti_files" in stix_object:
                for file in stix_object["x_opencti_files"]:
                    self.opencti.stix_cyber_observable.add_file(
                        id=stix_observable_result["id"],
                        file_name=file["name"],
                        data=base64.b64decode(file["data"]),
                        mime_type=file["mime_type"],
                        no_trigger_import=file.get("no_trigger_import", False),
                    )
            if (
                self.opencti.get_attribute_in_extension("files", stix_object)
                is not None
            ):
                for file in self.opencti.get_attribute_in_extension(
                    "files", stix_object
                ):
                    self.opencti.stix_cyber_observable.add_file(
                        id=stix_observable_result["id"],
                        file_name=file["name"],
                        data=base64.b64decode(file["data"]),
                        mime_type=file["mime_type"],
                        no_trigger_import=file.get("no_trigger_import", False),
                    )
            if "id" in stix_object:
                self.mapping_cache[stix_object["id"]] = {
                    "id": stix_observable_result["id"],
                    "type": stix_observable_result["entity_type"],
                }
            self.mapping_cache[stix_observable_result["id"]] = {
                "id": stix_observable_result["id"],
                "type": stix_observable_result["entity_type"],
            }
            # Iterate over refs to create appropriate relationships
            for key in stix_object.keys():
                if key not in [
                    "created_by_ref",
                    "object_marking_refs",
                    "x_opencti_created_by_ref",
                ]:
                    if key.endswith("_ref"):
                        relationship_type = key.replace("_ref", "")
                        if relationship_type.startswith("x_opencti_"):
                            relationship_type = relationship_type.split(
                                "x_opencti_", 1
                            )[1]
                            relationship_type = relationship_type.replace("_", "-")
                            relationship_type = "x_opencti_" + relationship_type
                        else:
                            relationship_type = relationship_type.replace("_", "-")
                        self.opencti.stix_nested_ref_relationship.create(
                            fromId=stix_observable_result["id"],
                            toId=stix_object[key],
                            relationship_type=relationship_type,
                        )
                    elif key.endswith("_refs"):
                        relationship_type = key.replace("_refs", "")
                        if relationship_type.startswith("x_opencti_"):
                            relationship_type = relationship_type.split(
                                "x_opencti_", 1
                            )[1]
                            relationship_type = relationship_type.replace("_", "-")
                            relationship_type = "x_opencti_" + relationship_type
                        else:
                            relationship_type = relationship_type.replace("_", "-")
                        for value in stix_object[key]:
                            self.opencti.stix_nested_ref_relationship.create(
                                fromId=stix_observable_result["id"],
                                toId=value,
                                relationship_type=relationship_type,
                            )
        else:
            return None

    def import_relationship(
        self, stix_relation: Dict, update: bool = False, types: List = None
    ) -> None:
        # Extract
        embedded_relationships = self.extract_embedded_relationships(
            stix_relation, types
        )
        created_by_id = embedded_relationships["created_by"]
        object_marking_ids = embedded_relationships["object_marking"]
        object_label_ids = embedded_relationships["object_label"]
        open_vocabs = embedded_relationships["open_vocabs"]
        granted_refs_ids = embedded_relationships["granted_refs"]
        kill_chain_phases_ids = embedded_relationships["kill_chain_phases"]
        object_refs_ids = embedded_relationships["object_refs"]
        external_references_ids = embedded_relationships["external_references"]
        reports = embedded_relationships["reports"]

        # Extra
        extras = {
            "created_by_id": created_by_id,
            "object_marking_ids": object_marking_ids,
            "object_label_ids": object_label_ids,
            "open_vocabs": open_vocabs,
            "granted_refs_ids": granted_refs_ids,
            "kill_chain_phases_ids": kill_chain_phases_ids,
            "object_ids": object_refs_ids,
            "external_references_ids": external_references_ids,
            "reports": reports,
        }

        # Create the relation

        ## Try to guess start_time / stop_time from external reference
        date = None
        if "external_references" in stix_relation:
            for external_reference in stix_relation["external_references"]:
                try:
                    if "description" in external_reference:
                        matches = datefinder.find_dates(
                            external_reference["description"],
                            base_date=datetime.datetime.fromtimestamp(0),
                        )
                    else:
                        matches = datefinder.find_dates(
                            external_reference["source_name"],
                            base_date=datetime.datetime.fromtimestamp(0),
                        )
                except:
                    matches = None
                date = None
                yesterday = datetime.datetime.now() - datetime.timedelta(days=1)
                if matches is not None:
                    try:
                        for match in matches:
                            if (
                                match.timestamp() < yesterday.timestamp()
                                and len(str(match.year)) == 4
                            ):
                                date = match.strftime("%Y-%m-%dT%H:%M:%SZ")
                                break
                    except:
                        date = None

        stix_relation_result = self.opencti.stix_core_relationship.import_from_stix2(
            stixRelation=stix_relation, extras=extras, update=update, defaultDate=date
        )
        if stix_relation_result is not None:
            self.mapping_cache[stix_relation["id"]] = {
                "id": stix_relation_result["id"],
                "type": stix_relation_result["entity_type"],
            }
        else:
            return None

        # Add external references
        for external_reference_id in external_references_ids:
            if external_reference_id in reports:
                self.opencti.report.add_stix_object_or_stix_relationship(
                    id=reports[external_reference_id]["id"],
                    stixObjectOrStixRelationshipId=stix_relation_result["id"],
                )
                self.opencti.report.add_stix_object_or_stix_relationship(
                    id=reports[external_reference_id]["id"],
                    stixObjectOrStixRelationshipId=stix_relation["source_ref"],
                )
                self.opencti.report.add_stix_object_or_stix_relationship(
                    id=reports[external_reference_id]["id"],
                    stixObjectOrStixRelationshipId=stix_relation["target_ref"],
                )

    def import_sighting(
        self,
        stix_sighting: Dict,
        from_id: str,
        to_id: str,
        update: bool = False,
        types: List = None,
    ) -> None:
        # Extract
        embedded_relationships = self.extract_embedded_relationships(
            stix_sighting, types
        )
        created_by_id = embedded_relationships["created_by"]
        object_marking_ids = embedded_relationships["object_marking"]
        object_label_ids = embedded_relationships["object_label"]
        open_vocabs = embedded_relationships["open_vocabs"]
        granted_refs_ids = embedded_relationships["granted_refs"]
        kill_chain_phases_ids = embedded_relationships["kill_chain_phases"]
        object_refs_ids = embedded_relationships["object_refs"]
        external_references_ids = embedded_relationships["external_references"]
        reports = embedded_relationships["reports"]

        # Extra
        extras = {
            "created_by_id": created_by_id,
            "object_marking_ids": object_marking_ids,
            "object_label_ids": object_label_ids,
            "open_vocabs": open_vocabs,
            "granted_refs_ids": granted_refs_ids,
            "kill_chain_phases_ids": kill_chain_phases_ids,
            "object_ids": object_refs_ids,
            "external_references_ids": external_references_ids,
            "reports": reports,
        }

        # Create the sighting

        ### Get the FROM
        if from_id in self.mapping_cache:
            final_from_id = self.mapping_cache[from_id]["id"]
        else:
            stix_object_result = (
                self.opencti.opencti_stix_object_or_stix_relationship.read(id=from_id)
            )
            if stix_object_result is not None:
                final_from_id = stix_object_result["id"]
            else:
                API_LOGGER.error("From ref of the sighting not found, doing nothing...")
                return None

        ### Get the TO
        final_to_id = None
        if to_id:
            if to_id in self.mapping_cache:
                final_to_id = self.mapping_cache[to_id]["id"]
            else:
                stix_object_result = (
                    self.opencti.opencti_stix_object_or_stix_relationship.read(id=to_id)
                )
                if stix_object_result is not None:
                    final_to_id = stix_object_result["id"]
                else:
                    API_LOGGER.error(
                        "To ref of the sighting not found, doing nothing..."
                    )
                    return None
        if (
            "x_opencti_negative" not in stix_sighting
            and self.opencti.get_attribute_in_extension("negative", stix_sighting)
            is not None
        ):
            stix_sighting[
                "x_opencti_negative"
            ] = self.opencti.get_attribute_in_extension("negative", stix_sighting)
        stix_sighting_result = self.opencti.stix_sighting_relationship.create(
            fromId=final_from_id,
            toId=final_to_id,
            stix_id=stix_sighting["id"] if "id" in stix_sighting else None,
            description=self.convert_markdown(stix_sighting["description"])
            if "description" in stix_sighting
            else None,
            first_seen=stix_sighting["first_seen"]
            if "first_seen" in stix_sighting
            else None,
            last_seen=stix_sighting["last_seen"]
            if "last_seen" in stix_sighting
            else None,
            count=stix_sighting["count"] if "count" in stix_sighting else 1,
            x_opencti_negative=stix_sighting["x_opencti_negative"]
            if "x_opencti_negative" in stix_sighting
            else False,
            created=stix_sighting["created"] if "created" in stix_sighting else None,
            modified=stix_sighting["modified"] if "modified" in stix_sighting else None,
            confidence=stix_sighting["confidence"]
            if "confidence" in stix_sighting
            else 15,
            createdBy=extras["created_by_id"] if "created_by_id" in extras else None,
            objectMarking=extras["object_marking_ids"]
            if "object_marking_ids" in extras
            else [],
            objectLabel=extras["object_label_ids"]
            if "object_label_ids" in extras
            else None,
            externalReferences=extras["external_references_ids"]
            if "external_references_ids" in extras
            else None,
            objectOrganization=extras["granted_refs_ids"]
            if "granted_refs_ids" in extras
            else [],
            update=update,
            ignore_dates=stix_sighting["x_opencti_ignore_dates"]
            if "x_opencti_ignore_dates" in stix_sighting
            else None,
        )
        if stix_sighting_result is not None:
            self.mapping_cache[stix_sighting["id"]] = {
                "id": stix_sighting_result["id"],
                "type": stix_sighting_result["entity_type"],
            }
        else:
            return None

    # endregion

    # region export
    def generate_export(self, entity: Dict, no_custom_attributes: bool = False) -> Dict:
        # Handle model deviation
        original_entity_type = entity["entity_type"]

        # Identities
        if IdentityTypes.has_value(entity["entity_type"]):
            entity["entity_type"] = "Identity"

        # Threat-Actors
        if ThreatActorTypes.has_value(entity["entity_type"]):
            if not no_custom_attributes:
                entity["x_opencti_type"] = entity["entity_type"]
            if entity["entity_type"] == "Threat-Actor-Group":
                entity["threat_actor_group"] = entity["name"]
            entity["entity_type"] = "Threat-Actor"

        # Locations
        if LocationTypes.has_value(entity["entity_type"]):
            if not no_custom_attributes:
                entity["x_opencti_location_type"] = entity["entity_type"]
            if entity["entity_type"] == "City":
                entity["city"] = entity["name"]
            elif entity["entity_type"] == "Country":
                entity["country"] = entity["name"]
            elif entity["entity_type"] == "Region":
                entity["region"] = entity["name"]
            entity["entity_type"] = "Location"

        # Malware
        if entity["entity_type"] == "Malware":
            if "is_family" not in entity or not isinstance(entity["is_family"], bool):
                entity["is_family"] = True

        # Threat Actor
        if entity["entity_type"] == "Threat-Actor-Group":
            entity["entity_type"] = "Threat-Actor"
        if entity["entity_type"] == "Threat-Actor-Individual":
            entity["entity_type"] = "Threat-Actor"

        # Files
        if entity["entity_type"] == "StixFile":
            entity["entity_type"] = "File"

        # Case Incident
        if entity["entity_type"] == "Case-Incident":
            entity["standard_id"] = "x-opencti-" + entity["standard_id"]
            entity["entity_type"] = "x-opencti-" + entity["entity_type"]

        # Case RFI
        if entity["entity_type"] == "Case-Rfi":
            entity["standard_id"] = "x-opencti-" + entity["standard_id"]
            entity["entity_type"] = "x-opencti-" + entity["entity_type"]

        # Case RFT
        if entity["entity_type"] == "Case-Rft":
            entity["standard_id"] = "x-opencti-" + entity["standard_id"]
            entity["entity_type"] = "x-opencti-" + entity["entity_type"]

        # Feedback
        if entity["entity_type"] == "Feedback":
            entity["standard_id"] = "x-opencti-" + entity["standard_id"]
            entity["entity_type"] = "x-opencti-" + entity["entity_type"]

        # Task
        if entity["entity_type"] == "Task":
            entity["standard_id"] = "x-opencti-" + entity["standard_id"]
            entity["entity_type"] = "x-opencti-" + entity["entity_type"]

        # Data component
        if entity["entity_type"] == "Data-Component":
            entity["standard_id"] = "x-mitre-" + entity["standard_id"]
            entity["entity_type"] = "x-mitre-" + entity["entity_type"]

        # Data source
        if entity["entity_type"] == "Data-Source":
            entity["standard_id"] = "x-mitre-" + entity["standard_id"]
            entity["entity_type"] = "x-mitre-" + entity["entity_type"]
            if "platforms" in entity and entity["platforms"] is not None:
                entity["x_mitre_platforms"] = entity["platforms"]
                del entity["platforms"]
            if (
                "collection_layers" in entity
                and entity["collection_layers"] is not None
            ):
                entity["x_mitre_collection_layers"] = entity["collection_layers"]
                del entity["collection_layers"]

        # Dates
        if (
            "valid_from" in entity
            and "valid_until" in entity
            and entity["valid_from"] == entity["valid_until"]
        ):
            del entity["valid_from"]

        # Flatten
        if "objectLabel" in entity and len(entity["objectLabel"]) > 0:
            entity["labels"] = []
            for object_label in entity["objectLabel"]:
                entity["labels"].append(object_label["value"])
        if "objectLabel" in entity:
            del entity["objectLabel"]
            del entity["objectLabelIds"]
        if (
            not no_custom_attributes
            and "killChainPhases" in entity
            and len(entity["killChainPhases"]) > 0
        ):
            entity["kill_chain_phases"] = []
            for object_kill_chain_phase in entity["killChainPhases"]:
                kill_chain_phase = {
                    "kill_chain_name": object_kill_chain_phase["kill_chain_name"],
                    "phase_name": object_kill_chain_phase["phase_name"],
                    "x_opencti_order": object_kill_chain_phase["x_opencti_order"],
                }
                entity["kill_chain_phases"].append(kill_chain_phase)
        if "killChainPhases" in entity:
            del entity["killChainPhases"]
            del entity["killChainPhasesIds"]
        if (
            not no_custom_attributes
            and "externalReferences" in entity
            and len(entity["externalReferences"]) > 0
        ):
            entity["external_references"] = []
            for entity_external_reference in entity["externalReferences"]:
                external_reference = dict()
                if self.opencti.not_empty(entity_external_reference["source_name"]):
                    external_reference["source_name"] = entity_external_reference[
                        "source_name"
                    ]
                if self.opencti.not_empty(entity_external_reference["description"]):
                    external_reference["description"] = entity_external_reference[
                        "description"
                    ]
                if self.opencti.not_empty(entity_external_reference["url"]):
                    external_reference["url"] = entity_external_reference["url"]
                if self.opencti.not_empty(entity_external_reference["hash"]):
                    external_reference["hash"] = entity_external_reference["hash"]
                if self.opencti.not_empty(entity_external_reference["external_id"]):
                    external_reference["external_id"] = entity_external_reference[
                        "external_id"
                    ]
                if (
                    "importFiles" in entity_external_reference
                    and len(entity_external_reference["importFiles"]) > 0
                ):
                    external_reference["x_opencti_files"] = []
                    for file in entity_external_reference["importFiles"]:
                        url = (
                            self.opencti.api_url.replace("graphql", "storage/get/")
                            + file["id"]
                        )
                        data = self.opencti.fetch_opencti_file(
                            url, binary=True, serialize=True
                        )
                        external_reference["x_opencti_files"].append(
                            {
                                "name": file["name"],
                                "data": data,
                                "mime_type": file["metaData"]["mimetype"],
                                "version": file["metaData"]["version"],
                            }
                        )
                entity["external_references"].append(external_reference)
        if "externalReferences" in entity:
            del entity["externalReferences"]
            del entity["externalReferencesIds"]
        if "indicators" in entity:
            del entity["indicators"]
            del entity["indicatorsIds"]
        if "hashes" in entity:
            hashes = entity["hashes"]
            entity["hashes"] = {}
            for hash in hashes:
                entity["hashes"][hash["algorithm"]] = hash["hash"]

        # Final
        entity["x_opencti_id"] = entity["id"]
        if not no_custom_attributes:
            entity["x_opencti_type"] = original_entity_type
        entity["id"] = entity["standard_id"]
        entity["type"] = entity["entity_type"].lower()
        del entity["standard_id"]
        del entity["entity_type"]
        del entity["parent_types"]
        if "created_at" in entity:
            del entity["created_at"]
        if "updated_at" in entity:
            del entity["updated_at"]

        return {k: v for k, v in entity.items() if self.opencti.not_empty(v)}

    def prepare_export(
        self,
        entity: Dict,
        mode: str = "simple",
        max_marking_definition_entity: Dict = None,
        no_custom_attributes: bool = False,
    ) -> List:
        if (
            self.check_max_marking_definition(
                max_marking_definition_entity,
                entity["objectMarking"] if "objectMarking" in entity else [],
            )
            is False
        ):
            API_LOGGER.info(
                "Marking definitions of %s are less than max definition, not exporting.",
                entity["type"],
            )
            return []
        result = []
        objects_to_get = []
        relations_to_get = []
        # CreatedByRef
        if (
            not no_custom_attributes
            and "createdBy" in entity
            and entity["createdBy"] is not None
        ):
            created_by = self.generate_export(entity["createdBy"])
            if entity["type"] in STIX_CYBER_OBSERVABLE_MAPPING:
                entity["x_opencti_created_by_ref"] = created_by["id"]
            else:
                entity["created_by_ref"] = created_by["id"]
            result.append(created_by)
        # Labels
        if entity["type"] in STIX_CYBER_OBSERVABLE_MAPPING and "labels" in entity:
            entity["x_opencti_labels"] = entity["labels"]
            del entity["labels"]

        if "createdBy" in entity:
            del entity["createdBy"]
            del entity["createdById"]
        if "observables" in entity:
            del entity["observables"]
            del entity["observablesIds"]
        if "creators" in entity:
            del entity["creators"]

        # DataSource
        if (
            not no_custom_attributes
            and "dataSource" in entity
            and entity["dataSource"] is not None
        ):
            data_source = self.generate_export(entity["dataSource"])
            entity["x_mitre_data_source_ref"] = data_source["id"]
            result.append(data_source)
        if "dataSource" in entity:
            del entity["dataSource"]
            del entity["dataSourceId"]

        # Dates
        if "first_seen" in entity and entity["first_seen"].startswith("1970"):
            del entity["first_seen"]
        if "start_time" in entity and entity["start_time"].startswith("1970"):
            del entity["start_time"]
        if "last_seen" in entity and entity["last_seen"].startswith("5138"):
            del entity["last_seen"]
        if "stop_time" in entity and entity["stop_time"].startswith("5138"):
            del entity["stop_time"]

        entity_copy = entity.copy()
        if no_custom_attributes:
            if "external_references" in entity:
                del entity["external_references"]
            for key in entity_copy.keys():
                if key.startswith("x_"):
                    del entity[key]
            entity["x_opencti_id"] = entity_copy["x_opencti_id"]
        # ObjectMarkingRefs
        if (
            not no_custom_attributes
            and "objectMarking" in entity
            and len(entity["objectMarking"]) > 0
        ):
            entity["object_marking_refs"] = []
            for entity_marking_definition in entity["objectMarking"]:
                if entity_marking_definition["definition_type"] == "TLP":
                    created = "2017-01-20T00:00:00.000Z"
                else:
                    created = entity_marking_definition["created"]
                marking_definition = {
                    "type": "marking-definition",
                    "spec_version": SPEC_VERSION,
                    "id": entity_marking_definition["standard_id"],
                    "created": created,
                    "definition_type": entity_marking_definition[
                        "definition_type"
                    ].lower(),
                    "name": entity_marking_definition["definition"],
                    "definition": {
                        entity_marking_definition["definition_type"]
                        .lower(): entity_marking_definition["definition"]
                        .lower()
                        .replace("tlp:", "")
                    },
                }
                result.append(marking_definition)
                entity["object_marking_refs"].append(marking_definition["id"])
        if "objectMarking" in entity:
            del entity["objectMarking"]
            del entity["objectMarkingIds"]
        # ObjectRefs
        if (
            not no_custom_attributes
            and "objects" in entity
            and len(entity["objects"]) > 0
        ):
            entity["object_refs"] = []
            objects_to_get = entity["objects"]
            for entity_object in entity["objects"]:
                if (
                    entity["type"] == "report"
                    and entity_object["entity_type"]
                    not in [
                        "Note",
                        "Report",
                        "Opinion",
                    ]
                    and "stix-ref-relationship" not in entity_object["parent_types"]
                ):
                    entity["object_refs"].append(entity_object["standard_id"])
                elif (
                    entity["type"] == "note"
                    and entity_object["entity_type"]
                    not in [
                        "Note",
                        "Opinion",
                    ]
                    and "stix-ref-relationship" not in entity_object["parent_types"]
                ):
                    entity["object_refs"].append(entity_object["standard_id"])
                elif (
                    entity["type"] == "opinion"
                    and entity_object["entity_type"] not in ["Opinion"]
                    and "stix-ref-relationship" not in entity_object["parent_types"]
                ):
                    entity["object_refs"].append(entity_object["standard_id"])
                elif (
                    entity["type"] == "observed-data"
                    and "stix-ref-relationship" not in entity_object["parent_types"]
                ):
                    entity["object_refs"].append(entity_object["standard_id"])
                elif (
                    entity["type"] == "grouping"
                    and "stix-ref-relationship" not in entity_object["parent_types"]
                ):
                    entity["object_refs"].append(entity_object["standard_id"])
                elif (
                    entity["type"] == "x-opencti-case-incident"
                    and "stix-ref-relationship" not in entity_object["parent_types"]
                ):
                    entity["object_refs"].append(entity_object["standard_id"])
                elif (
                    entity["type"] == "x-opencti-feedback"
                    and "stix-ref-relationship" not in entity_object["parent_types"]
                ):
                    entity["object_refs"].append(entity_object["standard_id"])
                elif (
                    entity["type"] == "x-opencti-case-rfi"
                    and "stix-ref-relationship" not in entity_object["parent_types"]
                ):
                    entity["object_refs"].append(entity_object["standard_id"])
                elif (
                    entity["type"] == "x-opencti-case-rft"
                    and "stix-ref-relationship" not in entity_object["parent_types"]
                ):
                    entity["object_refs"].append(entity_object["standard_id"])
                elif (
                    entity["type"] == "x-opencti-task"
                    and "stix-ref-relationship" not in entity_object["parent_types"]
                ):
                    entity["object_refs"].append(entity_object["standard_id"])
        if "objects" in entity:
            del entity["objects"]
            del entity["objectsIds"]
        # Stix Sighting Relationship
        if entity["type"] == "stix-sighting-relationship":
            entity["type"] = "sighting"
            entity["count"] = entity["attribute_count"]
            del entity["attribute_count"]
            entity["sighting_of_ref"] = entity["from"]["standard_id"]
            objects_to_get.append(entity["from"])
            entity["where_sighted_refs"] = [entity["to"]["standard_id"]]
            objects_to_get.append(entity["to"])
            del entity["from"]
            del entity["to"]
        # Stix Core Relationship
        if "from" in entity or "to" in entity:
            entity["type"] = "relationship"
        if "from" in entity:
            entity["source_ref"] = entity["from"]["standard_id"]
            objects_to_get.append(entity["from"])
            del entity["from"]
        if "to" in entity:
            entity["target_ref"] = entity["to"]["standard_id"]
            objects_to_get.append(entity["to"])
            del entity["to"]
        # Stix Domain Object
        if "attribute_abstract" in entity:
            entity["abstract"] = entity["attribute_abstract"]
            del entity["attribute_abstract"]
        # Stix Cyber Observable
        if "observable_value" in entity:
            del entity["observable_value"]
        if "attribute_key" in entity:
            entity["key"] = entity["attribute_key"]
            del entity["attribute_key"]
        if "attribute_date" in entity:
            entity["date"] = entity["attribute_date"]
            del entity["attribute_date"]
        # Artifact
        if entity["type"] == "artifact" and "importFiles" in entity:
            first_file = entity["importFiles"][0]["id"]
            url = self.opencti.api_url.replace("graphql", "storage/get/") + first_file
            file = self.opencti.fetch_opencti_file(url, binary=True, serialize=True)
            if file:
                entity["payload_bin"] = file
        # Files
        if "importFiles" in entity and len(entity["importFiles"]) > 0:
            entity["x_opencti_files"] = []
            for file in entity["importFiles"]:
                url = (
                    self.opencti.api_url.replace("graphql", "storage/get/") + file["id"]
                )
                data = self.opencti.fetch_opencti_file(url, binary=True, serialize=True)
                entity["x_opencti_files"].append(
                    {
                        "name": file["name"],
                        "data": data,
                        "mime_type": file["metaData"]["mimetype"],
                        "version": file["metaData"]["version"],
                    }
                )
            del entity["importFiles"]
            del entity["importFilesIds"]

        # StixRefRelationship
        stix_nested_ref_relationships = self.opencti.stix_nested_ref_relationship.list(
            fromId=entity["x_opencti_id"]
        )
        for stix_nested_ref_relationship in stix_nested_ref_relationships:
            if "standard_id" in stix_nested_ref_relationship["to"]:
                # dirty fix because the sample and operating-system ref are not multiple for a Malware Analysis
                # will be replaced by a proper toStix converter in the back
                if not MultipleRefRelationship.has_value(
                    stix_nested_ref_relationship["relationship_type"]
                ) or (
                    entity["type"] == "malware-analysis"
                    and stix_nested_ref_relationship["relationship_type"]
                    in ["operating-system", "sample"]
                ):
                    key = (
                        stix_nested_ref_relationship["relationship_type"]
                        .replace("obs_", "")
                        .replace("-", "_")
                        + "_ref"
                    )
                    entity[key] = stix_nested_ref_relationship["to"]["standard_id"]

                else:
                    key = (
                        stix_nested_ref_relationship["relationship_type"]
                        .replace("obs_", "")
                        .replace("-", "_")
                        + "_refs"
                    )
                    if key in entity and isinstance(entity[key], list):
                        entity[key].append(
                            stix_nested_ref_relationship["to"]["standard_id"]
                        )
                    else:
                        entity[key] = [
                            stix_nested_ref_relationship["to"]["standard_id"]
                        ]
        result.append(entity)

        if mode == "simple":
            if no_custom_attributes:
                del entity["x_opencti_id"]
            return result
        elif mode == "full":
            uuids = [entity["id"]]
            for x in result:
                uuids.append(x["id"])
            # Get extra refs
            for key in entity.keys():
                if key.endswith("_ref"):
                    type = entity[key].split("--")[0]
                    if type in STIX_CYBER_OBSERVABLE_MAPPING:
                        objects_to_get.append(
                            {
                                "id": entity[key],
                                "entity_type": "Stix-Cyber-Observable",
                                "parent_types": ["Stix-Cyber-Observable"],
                            }
                        )
                    else:
                        objects_to_get.append(
                            {
                                "id": entity[key],
                                "entity_type": "Stix-Domain-Object",
                                "parent_types": ["Stix-Domain-Object"],
                            }
                        )
                elif key.endswith("_refs"):
                    for value in entity[key]:
                        type = value.split("--")[0]
                        if type in STIX_CYBER_OBSERVABLE_MAPPING:
                            objects_to_get.append(
                                {
                                    "id": value,
                                    "entity_type": "Stix-Cyber-Observable",
                                    "parent_types": ["Stix-Cyber-Observable"],
                                }
                            )
                        else:
                            objects_to_get.append(
                                {
                                    "id": value,
                                    "entity_type": "Stix-Domain-Object",
                                    "parent_types": ["Stix-Domain-Object"],
                                }
                            )
            # Get extra relations (from AND to)
            stix_core_relationships = self.opencti.stix_core_relationship.list(
                elementId=entity["x_opencti_id"], getAll=True
            )
            for stix_core_relationship in stix_core_relationships:
                if self.check_max_marking_definition(
                    max_marking_definition_entity,
                    stix_core_relationship["objectMarking"]
                    if "objectMarking" in stix_core_relationship
                    else None,
                ):
                    objects_to_get.append(
                        stix_core_relationship["to"]
                        if stix_core_relationship["to"]["id"] != entity["x_opencti_id"]
                        else stix_core_relationship["from"]
                    )
                    relation_object_data = self.prepare_export(
                        self.generate_export(stix_core_relationship),
                        "simple",
                        max_marking_definition_entity,
                    )
                    relation_object_bundle = self.filter_objects(
                        uuids, relation_object_data
                    )
                    uuids = uuids + [x["id"] for x in relation_object_bundle]
                    result = result + relation_object_bundle
                else:
                    API_LOGGER.info(
                        'Marking definitions of %s "%s" are less than max definition, '
                        "not exporting the relation AND the target entity.",
                        *(
                            stix_core_relationship["entity_type"],
                            stix_core_relationship["id"],
                        ),
                    )
            # Get sighting
            stix_sighting_relationships = self.opencti.stix_sighting_relationship.list(
                elementId=entity["x_opencti_id"],
                getAll=True,
            )
            for stix_sighting_relationship in stix_sighting_relationships:
                if self.check_max_marking_definition(
                    max_marking_definition_entity,
                    stix_sighting_relationship["objectMarking"]
                    if "objectMarking" in stix_sighting_relationship
                    else None,
                ):
                    objects_to_get.append(
                        stix_sighting_relationship["to"]
                        if stix_sighting_relationship["to"]["id"]
                        != entity["x_opencti_id"]
                        else stix_sighting_relationship["from"]
                    )
                    relation_object_data = self.prepare_export(
                        self.generate_export(stix_sighting_relationship),
                        "simple",
                        max_marking_definition_entity,
                    )
                    relation_object_bundle = self.filter_objects(
                        uuids, relation_object_data
                    )
                    uuids = uuids + [x["id"] for x in relation_object_bundle]
                    result = result + relation_object_bundle
                else:
                    API_LOGGER.info(
                        'Marking definitions of %s "%s" are less than max definition, '
                        "not exporting the relation AND the target entity.",
                        stix_sighting_relationship["entity_type"],
                        stix_sighting_relationship["id"],
                    )
            if no_custom_attributes:
                del entity["x_opencti_id"]
            # Export
            reader = {
                "Attack-Pattern": self.opencti.attack_pattern.read,
                "Campaign": self.opencti.campaign.read,
                "Channel": self.opencti.channel.read,
                "Note": self.opencti.note.read,
                "Observed-Data": self.opencti.observed_data.read,
                "Opinion": self.opencti.opinion.read,
                "Report": self.opencti.report.read,
                "Case-Incident": self.opencti.case_incident.read,
                "Feedback": self.opencti.feedback.read,
                "Case-Rfi": self.opencti.case_rfi.read,
                "Case-Rft": self.opencti.case_rft.read,
                "Task": self.opencti.task.read,
                "Course-Of-Action": self.opencti.course_of_action.read,
                "Data-Component": self.opencti.data_component.read,
                "Data-Source": self.opencti.data_source.read,
                "Identity": self.opencti.identity.read,
                "Indicator": self.opencti.indicator.read,
                "Infrastructure": self.opencti.infrastructure.read,
                "Intrusion-Set": self.opencti.intrusion_set.read,
                "Location": self.opencti.location.read,
                "Language": self.opencti.language.read,
                "Malware": self.opencti.malware.read,
                "Malware-Analysis": self.opencti.malware_analysis.read,
                "Threat-Actor": self.opencti.threat_actor.read,
                "Threat-Actor-Group": self.opencti.threat_actor_group.read,
                "Threat-Actor-Individual": self.opencti.threat_actor_individual.read,
                "Tool": self.opencti.tool.read,
                "Vulnerability": self.opencti.vulnerability.read,
                "Incident": self.opencti.incident.read,
                "Stix-Core-Object": self.opencti.stix_core_object.read,
                "Stix-Cyber-Observable": self.opencti.stix_cyber_observable.read,
                "Stix-Domain-Object": self.opencti.stix_domain_object.read,
                "stix-core-relationship": self.opencti.stix_core_relationship.read,
                "stix-sighting-relationship": self.opencti.stix_sighting_relationship.read,
            }
            # Get extra objects
            for entity_object in objects_to_get:
                # Map types
                if entity_object["entity_type"] == "StixFile":
                    entity_object["entity_type"] = "File"

                if IdentityTypes.has_value(entity_object["entity_type"]):
                    entity_object["entity_type"] = "Identity"
                elif LocationTypes.has_value(entity_object["entity_type"]):
                    entity_object["entity_type"] = "Location"
                elif StixCyberObservableTypes.has_value(entity_object["entity_type"]):
                    entity_object["entity_type"] = "Stix-Cyber-Observable"
                elif "stix-core-relationship" in entity_object["parent_types"]:
                    entity_object["entity_type"] = "stix-core-relationship"
                elif "stix-ref-relationship" in entity_object["parent_types"]:
                    entity_object["entity_type"] = "stix-ref-relationship"

                do_read = reader.get(
                    entity_object["entity_type"],
                    lambda **kwargs: self.unknown_type(
                        {"type": entity_object["entity_type"]}
                    ),
                )
                entity_object_data = do_read(id=entity_object["id"])
                if entity_object_data is not None:
                    stix_entity_object = self.prepare_export(
                        self.generate_export(entity_object_data),
                        "simple",
                        max_marking_definition_entity,
                    )
                    # Add to result
                    entity_object_bundle = self.filter_objects(
                        uuids, stix_entity_object
                    )
                    uuids = uuids + [x["id"] for x in entity_object_bundle]
                    result = result + entity_object_bundle
            for relation_object in relations_to_get:
                relation_object_data = self.prepare_export(
                    self.opencti.stix_core_relationship.read(id=relation_object["id"])
                )
                relation_object_bundle = self.filter_objects(
                    uuids, relation_object_data
                )
                uuids = uuids + [x["id"] for x in relation_object_bundle]
                result = result + relation_object_bundle

            # Get extra reports
            """
            for uuid in uuids:
                if "marking-definition" not in uuid:
                    reports = self.opencti.opencti_stix_object_or_stix_relationship.reports(id=uuid)
                    for report in reports:
                        report_object_data = self.opencti.report.to_stix2(
                            entity=report,
                            mode="simple",
                            max_marking_definition_entity=max_marking_definition_entity,
                        )
                        report_object_bundle = self.filter_objects(
                            uuids, report_object_data
                        )
                        uuids = uuids + [x["id"] for x in report_object_bundle]
                        result = result + report_object_bundle
            """

            # Get notes
            # for export_uuid in uuids:
            #    if "marking-definition" not in export_uuid:
            #        notes = self.opencti.opencti_stix_object_or_stix_relationship.notes(
            #            id=export_uuid
            #        )
            #        for note in notes:
            #            note_object_data = self.opencti.note.to_stix2(
            #                entity=note,
            #                mode="simple",
            #                max_marking_definition_entity=max_marking_definition_entity,
            #            )
            #            note_object_bundle = self.filter_objects(
            #                uuids, note_object_data
            #            )
            #            uuids = uuids + [x["id"] for x in note_object_bundle]
            #            result = result + note_object_bundle

            # Refilter all the reports object refs
            final_result = []
            for entity in result:
                if entity["type"] in [
                    "report",
                    "note",
                    "opinion",
                    "observed-data",
                    "grouping",
                ]:
                    if "object_refs" in entity:
                        entity["object_refs"] = [
                            k for k in entity["object_refs"] if k in uuids
                        ]
                    final_result.append(entity)
                else:
                    final_result.append(entity)
            return final_result
        else:
            return []

    def export_entity(
        self,
        entity_type: str,
        entity_id: str,
        mode: str = "simple",
        max_marking_definition: Dict = None,
        no_custom_attributes: bool = False,
        only_entity: bool = False,
    ) -> Dict:
        max_marking_definition_entity = (
            self.opencti.marking_definition.read(id=max_marking_definition)
            if max_marking_definition is not None
            else None
        )
        bundle = {
            "type": "bundle",
            "id": "bundle--" + str(uuid.uuid4()),
            "objects": [],
        }

        if entity_type == "StixFile":
            entity_type = "File"

        # Map types
        if IdentityTypes.has_value(entity_type):
            entity_type = "Identity"
        if LocationTypes.has_value(entity_type):
            entity_type = "Location"

        # Reader
        reader = {
            "Attack-Pattern": self.opencti.attack_pattern.read,
            "Campaign": self.opencti.campaign.read,
            "Channel": self.opencti.channel.read,
            "Event": self.opencti.campaign.read,
            "Note": self.opencti.note.read,
            "Observed-Data": self.opencti.observed_data.read,
            "Opinion": self.opencti.opinion.read,
            "Report": self.opencti.report.read,
            "Grouping": self.opencti.grouping.read,
            "Case-Incident": self.opencti.case_incident.read,
            "Feedback": self.opencti.feedback.read,
            "Case-Rfi": self.opencti.case_rfi.read,
            "Case-Rft": self.opencti.case_rft.read,
            "Task": self.opencti.task.read,
            "Course-Of-Action": self.opencti.course_of_action.read,
            "Data-Component": self.opencti.data_component.read,
            "Data-Source": self.opencti.data_source.read,
            "Identity": self.opencti.identity.read,
            "Indicator": self.opencti.indicator.read,
            "Infrastructure": self.opencti.infrastructure.read,
            "Intrusion-Set": self.opencti.intrusion_set.read,
            "Location": self.opencti.location.read,
            "Language": self.opencti.language.read,
            "Malware": self.opencti.malware.read,
            "Malware-Analysis": self.opencti.malware_analysis.read,
            "Threat-Actor": self.opencti.threat_actor.read,
            "Threat-Actor-Group": self.opencti.threat_actor_group.read,
            "Threat-Actor-Individual": self.opencti.threat_actor_individual.read,
            "Tool": self.opencti.tool.read,
            "Narrative": self.opencti.narrative.read,
            "Vulnerability": self.opencti.vulnerability.read,
            "Incident": self.opencti.incident.read,
            "Stix-Cyber-Observable": self.opencti.stix_cyber_observable.read,
            "stix-core-relationship": self.opencti.stix_core_relationship.read,
        }
        if StixCyberObservableTypes.has_value(entity_type):
            entity_type = "Stix-Cyber-Observable"
        do_read = reader.get(
            entity_type, lambda **kwargs: self.unknown_type({"type": entity_type})
        )
        entity = do_read(id=entity_id)
        if entity is None:
            API_LOGGER.error("Cannot export entity (not found)")
            return bundle
        entity_standard_id = entity["standard_id"]
        stix_objects = self.prepare_export(
            self.generate_export(entity, no_custom_attributes),
            mode,
            max_marking_definition_entity,
            no_custom_attributes,
        )
        if stix_objects is not None:
            bundle["objects"].extend(stix_objects)
        if only_entity:
            return [e for e in bundle["objects"] if e.get("id") == entity_standard_id][
                0
            ]
        return bundle

    def export_list(
        self,
        entity_type: str,
        search: Dict = None,
        filters: List = None,
        order_by: str = None,
        order_mode: str = None,
        mode: str = "simple",
        max_marking_definition: Dict = None,
        types: List = None,
        elementId: str = None,
        fromId: str = None,
        toId: str = None,
        elementWithTargetTypes: [str] = None,
        fromTypes: [str] = None,
        toTypes: [str] = None,
        relationship_type: [str] = None,
    ) -> Dict:
        max_marking_definition_entity = (
            self.opencti.marking_definition.read(id=max_marking_definition)
            if max_marking_definition is not None
            else None
        )
        bundle = {
            "type": "bundle",
            "id": "bundle--" + str(uuid.uuid4()),
            "objects": [],
        }

        if IdentityTypes.has_value(entity_type):
            if filters is not None:
                filters.filters.append({"key": "entity_type", "values": [entity_type]})
            else:
                filters = {
                    "mode": "and",
                    "filters": [{"key": "entity_type", "values": [entity_type]}],
                    "filterGroups": [],
                }
            entity_type = "Identity"

        if LocationTypes.has_value(entity_type):
            if filters is not None:
                filters.filters.append({"key": "entity_type", "values": [entity_type]})
            else:
                filters = {
                    "mode": "and",
                    "filters": [{"key": "entity_type", "values": [entity_type]}],
                    "filterGroups": [],
                }
            entity_type = "Location"

        if StixCyberObservableTypes.has_value(entity_type):
            if filters is not None:
                filters.filters.append({"key": "entity_type", "values": [entity_type]})
            else:
                filters = {
                    "mode": "and",
                    "filters": [{"key": "entity_type", "values": [entity_type]}],
                    "filterGroups": [],
                }
            entity_type = "Stix-Cyber-Observable"

        if entity_type == "Container":
            if filters is not None:
                filters.filters.append(
                    {
                        "key": "entity_type",
                        "values": [
                            "Report",
                            "Grouping",
                            "Note",
                            "Observed-Data",
                            "Opinion",
                            "Case-Incident",
                            "Case-Rfi",
                            "Case-Rft",
                        ],
                    }
                )
            else:
                filters = {
                    "mode": "and",
                    "filters": [
                        {
                            "key": "entity_type",
                            "values": [
                                "Report",
                                "Grouping",
                                "Note",
                                "Observed-Data",
                                "Opinion",
                                "Case-Incident",
                                "Case-Rfi",
                                "Case-Rft",
                            ],
                        }
                    ],
                    "filterGroups": [],
                }
            entity_type = "Stix-Domain-Object"

        # List
        lister = {
            "Stix-Core-Object": self.opencti.stix_core_object.list,
            "Stix-Domain-Object": self.opencti.stix_domain_object.list,
            "Attack-Pattern": self.opencti.attack_pattern.list,
            "Campaign": self.opencti.campaign.list,
            "Channel": self.opencti.channel.list,
            "Event": self.opencti.event.list,
            "Note": self.opencti.note.list,
            "Observed-Data": self.opencti.observed_data.list,
            "Opinion": self.opencti.opinion.list,
            "Report": self.opencti.report.list,
            "Grouping": self.opencti.grouping.list,
            "Case-Incident": self.opencti.case_incident.list,
            "Feedback": self.opencti.feedback.list,
            "Case-Rfi": self.opencti.case_rfi.list,
            "Case-Rft": self.opencti.case_rft.list,
            "Task": self.opencti.task.list,
            "Course-Of-Action": self.opencti.course_of_action.list,
            "Data-Component": self.opencti.data_component.list,
            "Data-Source": self.opencti.data_source.list,
            "Identity": self.opencti.identity.list,
            "Indicator": self.opencti.indicator.list,
            "Infrastructure": self.opencti.infrastructure.list,
            "Intrusion-Set": self.opencti.intrusion_set.list,
            "Location": self.opencti.location.list,
            "Language": self.opencti.language.list,
            "Malware": self.opencti.malware.list,
            "Malware-Analysis": self.opencti.malware_analysis.list,
            "Threat-Actor": self.opencti.threat_actor_group.list,
            "Threat-Actor-Group": self.opencti.threat_actor_group.list,
            "Threat-Actor-Individual": self.opencti.threat_actor_individual.list,
            "Tool": self.opencti.tool.list,
            "Narrative": self.opencti.narrative.list,
            "Vulnerability": self.opencti.vulnerability.list,
            "Incident": self.opencti.incident.list,
            "Stix-Cyber-Observable": self.opencti.stix_cyber_observable.list,
            "stix-sighting-relationship": self.opencti.stix_sighting_relationship.list,
            "stix-core-relationship": self.opencti.stix_core_relationship.list,
        }
        do_list = lister.get(
            entity_type, lambda **kwargs: self.unknown_type({"type": entity_type})
        )
        entities_list = do_list(
            search=search,
            filters=filters,
            orderBy=order_by,
            orderMode=order_mode,
            types=types,
            getAll=True,
            elementId=elementId,
            fromId=fromId,
            toId=toId,
            elementWithTargetTypes=elementWithTargetTypes,
            fromTypes=fromTypes,
            toTypes=toTypes,
            relationship_type=relationship_type,
        )
        if entities_list is not None:
            uuids = []
            for entity in entities_list:
                entity_bundle = self.prepare_export(
                    self.generate_export(entity),
                    mode,
                    max_marking_definition_entity,
                )
                if entity_bundle is not None:
                    entity_bundle_filtered = self.filter_objects(uuids, entity_bundle)
                    for x in entity_bundle_filtered:
                        uuids.append(x["id"])
                    bundle["objects"] = bundle["objects"] + entity_bundle_filtered
        return bundle

    def export_selected(
        self,
        entities_list: [str],
        mode: str = "simple",
        max_marking_definition: Dict = None,
    ) -> Dict:
        max_marking_definition_entity = (
            self.opencti.marking_definition.read(id=max_marking_definition)
            if max_marking_definition is not None
            else None
        )
        bundle = {
            "type": "bundle",
            "id": "bundle--" + str(uuid.uuid4()),
            "objects": [],
        }
        if entities_list is not None:
            uuids = []
            for entity in entities_list:
                entity_bundle = self.prepare_export(
                    self.generate_export(entity),
                    mode,
                    max_marking_definition_entity,
                )
                if entity_bundle is not None:
                    entity_bundle_filtered = self.filter_objects(uuids, entity_bundle)
                    for x in entity_bundle_filtered:
                        uuids.append(x["id"])
                    bundle["objects"] = bundle["objects"] + entity_bundle_filtered
        return bundle

    def import_bundle(
        self,
        stix_bundle: Dict,
        update: bool = False,
        types: List = None,
        retry_number: int = None,
    ) -> List:
        # Check if the bundle is correctly formatted
        if "type" not in stix_bundle or stix_bundle["type"] != "bundle":
            raise ValueError("JSON data type is not a STIX2 bundle")
        if "objects" not in stix_bundle or len(stix_bundle["objects"]) == 0:
            raise ValueError("JSON data objects is empty")
        event_version = (
            stix_bundle["x_opencti_event_version"]
            if "x_opencti_event_version" in stix_bundle
            else None
        )
        if retry_number is not None:
            self.opencti.set_retry_number(retry_number)
        stix2_splitter = OpenCTIStix2Splitter()
        try:
            bundles = stix2_splitter.split_bundle(stix_bundle, False, event_version)
        except RecursionError:
            bundles = [stix_bundle]
        # Import every elements in a specific order
        imported_elements = []

        # Marking definitions
        for bundle in bundles:
            for item in bundle["objects"]:
                if "x_opencti_event_version" in bundle:
                    if bundle["x_opencti_event_version"] == "3":
                        if "x_opencti_patch" in item:
                            self.stix2_update.process_update(item)
                            continue
                if item["type"] == "relationship":
                    self.import_relationship(item, update, types)
                elif item["type"] == "sighting":
                    # Resolve the to
                    to_ids = []
                    if "where_sighted_refs" in item:
                        for where_sighted_ref in item["where_sighted_refs"]:
                            to_ids.append(where_sighted_ref)
                    # Import sighting_of_ref
                    if "x_opencti_sighting_of_ref" in item:
                        from_id = item["x_opencti_sighting_of_ref"]
                        if len(to_ids) > 0:
                            for to_id in to_ids:
                                self.import_sighting(item, from_id, to_id, update)
                    if (
                        self.opencti.get_attribute_in_extension("sighting_of_ref", item)
                        is not None
                    ):
                        from_id = self.opencti.get_attribute_in_extension(
                            "sighting_of_ref", item
                        )
                        if len(to_ids) > 0:
                            for to_id in to_ids:
                                self.import_sighting(item, from_id, to_id, update)
                    from_id = item["sighting_of_ref"]
                    if len(to_ids) > 0:
                        for to_id in to_ids:
                            self.import_sighting(item, from_id, to_id, update)
                    # Import observed_data_refs
                    if "observed_data_refs" in item:
                        for observed_data_ref in item["observed_data_refs"]:
                            if len(to_ids) > 0:
                                for to_id in to_ids:
                                    self.import_sighting(
                                        item, observed_data_ref, to_id, update
                                    )
                elif item["type"] == "label":
                    stix_ids = self.opencti.get_attribute_in_extension("stix_ids", item)
                    self.opencti.label.create(
                        stix_id=item["id"],
                        value=item["value"],
                        color=item["color"],
                        x_opencti_stix_ids=stix_ids,
                        update=update,
                    )
                elif item["type"] == "vocabulary":
                    stix_ids = self.opencti.get_attribute_in_extension("stix_ids", item)
                    self.opencti.vocabulary.create(
                        stix_id=item["id"],
                        name=item["name"],
                        category=item["category"],
                        description=item["description"]
                        if "description" in item
                        else None,
                        aliases=item["aliases"] if "aliases" in item else None,
                        x_opencti_stix_ids=stix_ids,
                        update=update,
                    )
                elif item["type"] == "external-reference":
                    stix_ids = self.opencti.get_attribute_in_extension("stix_ids", item)
                    self.opencti.external_reference.create(
                        stix_id=item["id"],
                        source_name=item["source_name"]
                        if "source_name" in item
                        else None,
                        url=item["url"] if "url" in item else None,
                        external_id=item["external_id"]
                        if "external_id" in item
                        else None,
                        description=item["description"]
                        if "description" in item
                        else None,
                        x_opencti_stix_ids=stix_ids,
                        update=update,
                    )
                elif item["type"] == "kill-chain-phase":
                    stix_ids = self.opencti.get_attribute_in_extension("stix_ids", item)
                    self.opencti.kill_chain_phase.create(
                        stix_id=item["id"],
                        kill_chain_name=item["kill_chain_name"],
                        phase_name=item["phase_name"],
                        x_opencti_order=item["order"] if "order" in item else 0,
                        x_opencti_stix_ids=stix_ids,
                        update=update,
                    )
                elif StixCyberObservableTypes.has_value(item["type"]):
                    if types is None or len(types) == 0:
                        self.import_observable(item, update, types)
                    elif item["type"] in types or "observable" in types:
                        self.import_observable(item, update, types)
                else:
                    # Check the scope
                    if (
                        item["type"] == "marking-definition"
                        or types is None
                        or len(types) == 0
                    ):
                        self.import_object(item, update, types)
                    # Handle identity & location if part of the scope
                    elif item["type"] in types:
                        self.import_object(item, update, types)
                    else:
                        # Specific OpenCTI scopes
                        if item["type"] == "identity":
                            if "identity_class" in item:
                                if ("class" in types or "sector" in types) and item[
                                    "identity_class"
                                ] == "class":
                                    self.import_object(item, update, types)
                                elif item["identity_class"] in types:
                                    self.import_object(item, update, types)
                        elif item["type"] == "location":
                            if "x_opencti_location_type" in item:
                                if item["x_opencti_location_type"].lower() in types:
                                    self.import_object(item, update, types)
                            elif (
                                self.opencti.get_attribute_in_extension(
                                    "location_type", item
                                )
                                is not None
                            ):
                                if (
                                    self.opencti.get_attribute_in_extension(
                                        "location_type", item
                                    ).lower()
                                    in types
                                ):
                                    self.import_object(item, update, types)
                imported_elements.append({"id": item["id"], "type": item["type"]})

        return imported_elements

    @staticmethod
    def put_attribute_in_extension(
        object, extension_id, key, value, multiple=False
    ) -> any:
        if ("x_opencti_" + key) in object:
            del object["x_opencti_" + key]
        if ("x_mitre_" + key) in object:
            del object["x_mitre_" + key]
        if "extensions" not in object:
            object["extensions"] = {}
        if extension_id not in object["extensions"]:
            object["extensions"][extension_id] = {}
        if key in object["extensions"][extension_id]:
            if multiple:
                object["extensions"][extension_id][key].append(value)
            else:
                object["extensions"][extension_id][key] = value
        else:
            if multiple:
                object["extensions"][extension_id][key] = [value]
            else:
                object["extensions"][extension_id][key] = value
        return object
