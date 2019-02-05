import copy
import math
from collections import defaultdict, Counter

import numpy as np

from urh.awre import AutoAssigner
from urh.awre.CommonRange import CommonRange, EmptyCommonRange, CommonRangeContainer
from urh.awre.Preprocessor import Preprocessor
from urh.awre.engines.AddressEngine import AddressEngine
from urh.awre.engines.LengthEngine import LengthEngine
from urh.awre.engines.SequenceNumberEngine import SequenceNumberEngine
from urh.cythonext import awre_util
from urh.signalprocessing.FieldType import FieldType
from urh.signalprocessing.Message import Message
from urh.signalprocessing.MessageType import MessageType
from urh.signalprocessing.ProtocoLabel import ProtocolLabel


class FormatFinder(object):
    MIN_MESSAGES_PER_CLUSTER = 2

    def __init__(self, messages, participants=None, shortest_field_length=None):
        """

        :type messages: list of Message
        :param participants:
        """
        if participants is not None:
            AutoAssigner.auto_assign_participants(messages, participants)

        existing_message_types_by_msg = {i: msg.message_type for i, msg in enumerate(messages)}
        self.existing_message_types = defaultdict(list)
        for i, message_type in existing_message_types_by_msg.items():
            self.existing_message_types[message_type].append(i)

        preprocessor = Preprocessor(self.get_bitvectors_from_messages(messages), existing_message_types_by_msg)
        self.preamble_starts, self.preamble_lengths, sync_len = preprocessor.preprocess()
        self.sync_ends = self.preamble_starts + self.preamble_lengths + sync_len

        if shortest_field_length is None:
            self.shortest_field_length = 8 if sync_len >= 8 else 4 if sync_len >= 4 else 1

        n = self.shortest_field_length
        for i, value in enumerate(self.sync_ends):
            # In doubt it is better to under estimate the sync end
            self.sync_ends[i] = n * max(int(math.floor((value - self.preamble_starts[i]) / n)), 1) + \
                                self.preamble_starts[i]

            if self.sync_ends[i] - self.preamble_starts[i] < self.preamble_lengths[i]:
                self.preamble_lengths[i] = self.sync_ends[i] - self.preamble_starts[i]

        self.bitvectors = self.get_bitvectors_from_messages(messages, self.sync_ends)
        self.hexvectors = self.get_hexvectors(self.bitvectors)
        self.xor_matrix = self.build_xor_matrix()
        participants = list(sorted(set(msg.participant for msg in messages)))
        self.participant_indices = [participants.index(msg.participant) for msg in messages]

    @property
    def message_types(self):
        """

        :rtype: list of MessageType
        """
        return list(self.existing_message_types.keys())

    def perform_iteration_for_message_type(self, message_type: MessageType):
        """
        Perform a field inference iteration for messages of the given message type
        This routine will return newly found fields as a set of Common Ranges

        :param message_type:
        :rtype: set of CommonRange
        """
        indices = self.existing_message_types[message_type]
        engines = []

        if not message_type.get_first_label_with_type(FieldType.Function.LENGTH):
            engines.append(LengthEngine([self.bitvectors[i] for i in indices]))
        if not message_type.get_first_label_with_type(FieldType.Function.DST_ADDRESS) \
                and not message_type.get_first_label_with_type(FieldType.Function.SRC_ADDRESS):
            engines.append(AddressEngine([self.hexvectors[i] for i in indices],
                                         [self.participant_indices[i] for i in indices]))
        if not message_type.get_first_label_with_type(FieldType.Function.SEQUENCE_NUMBER):
            engines.append(SequenceNumberEngine([self.bitvectors[i] for i in indices]))

        result = set()
        for engine in engines:
            high_scored_ranges = engine.find()  # type: list[CommonRange]
            high_scored_ranges = self.retransform_message_indices(high_scored_ranges, indices, self.sync_ends)
            merged_ranges = self.merge_common_ranges(high_scored_ranges)
            result.update(merged_ranges)
        return result

    def perform_iteration(self):
        for message_type in self.existing_message_types.copy():
            new_fields_for_message_type = self.perform_iteration_for_message_type(message_type)
            new_fields_for_message_type.update(
                self.get_preamble_and_sync(self.preamble_starts, self.preamble_lengths, self.sync_ends,
                                           message_type_indices=self.existing_message_types[message_type])
            )

            self.remove_overlapping_fields(new_fields_for_message_type, message_type)
            containers = self.create_common_range_containers(new_fields_for_message_type)
            if len(containers) == 1:
                for rng in containers[0]:
                    self.add_range_to_message_type(rng, message_type)
            elif len(containers) > 1:
                del self.existing_message_types[message_type]

                for i, container in enumerate(containers):
                    new_message_type = copy.deepcopy(message_type)  # type: MessageType
                    if i > 0:
                        new_message_type.name += " #{}".format(i)
                        new_message_type.give_new_id()

                    for rng in container:
                        self.add_range_to_message_type(rng, new_message_type)

                    self.existing_message_types[new_message_type].extend(list(container.message_indices))

    def build_xor_matrix(self):
        return awre_util.build_xor_matrix(self.bitvectors)

    @staticmethod
    def remove_overlapping_fields(common_ranges, message_type: MessageType):
        """
        Remove all fields from a set of CommonRanges which overlap with fields of the existing message type

        :type common_ranges: set of CommonRange
        :param message_type:
        :return:
        """
        for rng in common_ranges.copy():
            for lbl in message_type:  # type: ProtocolLabel
                if any(i in range(rng.bit_start, rng.bit_end) for i in range(lbl.start, lbl.end)):
                    common_ranges.discard(rng)
                    break

    @staticmethod
    def merge_common_ranges(common_ranges):
        """
        Merge common ranges if possible

        :type common_ranges: list of CommonRange
        :rtype: list of CommonRange
        """
        merged_ranges = []
        for common_range in common_ranges:
            assert isinstance(common_range, CommonRange)
            try:
                same_range = next(rng for rng in merged_ranges
                                  if rng.bit_start == common_range.bit_start
                                  and rng.bit_end == common_range.bit_end
                                  and rng.field_type == common_range.field_type)
                same_range.values.extend(common_range.values)
                same_range.message_indices.update(common_range.message_indices)
            except StopIteration:
                merged_ranges.append(common_range)

        return merged_ranges

    @staticmethod
    def add_range_to_message_type(common_range: CommonRange, message_type: MessageType):
        message_type.add_protocol_label(name=common_range.field_type,
                                        start=common_range.bit_start, end=common_range.bit_end,
                                        auto_created=True,
                                        type=FieldType.from_caption(common_range.field_type)
                                        )

    @staticmethod
    def get_hexvectors(bitvectors: list):
        result = []

        for bitvector in bitvectors:
            hexvector = np.empty(int(math.ceil(len(bitvector) / 4)), dtype=np.uint8)
            for i in range(0, len(hexvector)):
                bits = bitvector[4 * i:4 * (i + 1)]
                hexvector[i] = int("".join(map(str, bits)), 2)
            result.append(hexvector)

        return result

    @staticmethod
    def get_bitvectors_from_messages(messages: list, sync_ends: np.ndarray = None):
        if sync_ends is None:
            sync_ends = defaultdict(lambda: None)

        return [np.array(msg.decoded_bits[sync_ends[i]:], dtype=np.uint8) for i, msg in enumerate(messages)]

    @staticmethod
    def get_bitvectors_by_participant(messages: list) -> dict:
        result = defaultdict(list)
        for msg in messages:  # type: Message
            result[msg.participant].append(np.array(msg.decoded_bits, dtype=np.uint8))
        return result

    @staticmethod
    def create_common_range_containers(label_set: set, num_messages: int = None):
        """
        Create message types from set of labels.
        Handle overlapping conflicts and create multiple message types if needed

        :param label_set:
        :param num_messages:
        :return:
        :rtype: list of CommonRangeContainer
        """
        if num_messages is None:
            message_indices = sorted(set(i for rng in label_set for i in rng.message_indices))
        else:
            message_indices = range(num_messages)

        result = []
        for i in message_indices:
            labels = sorted(set(rng for rng in label_set if i in rng.message_indices
                                and not isinstance(rng, EmptyCommonRange)))

            container = next((container for container in result if container.has_same_ranges(labels)), None)
            if container is None:
                result.append(CommonRangeContainer(labels, message_indices={i}))
            else:
                container.message_indices.add(i)

        result = FormatFinder.handle_overlapping_conflict(result)

        return result

    @staticmethod
    def handle_overlapping_conflict(containers):
        """
        Handle overlapping conflicts for a list of CommonRangeContainers

        :type containers: list of CommonRangeContainer
        :return:
        """
        result = []
        for container in containers:
            if not container.ranges_overlap:
                result.append(container)
                continue

            result.append(FormatFinder.__handle_container_overlapping_conflict(container))

        return result

    @staticmethod
    def __handle_container_overlapping_conflict(container: CommonRangeContainer):
        """
        Handle overlapping conflict for a CommRangeContainer.
        We can assert that all labels in the container share the same message indices
        because we partitioned them in a step before.
        If two or more labels overlap we have three ways to resolve the conflict:

        1. Choose the range with the highest score
        2. If multiple ranges overlap choose the ranges that maximize the overall (cumulated) score
        3. If the overlapping is very small i.e. only 1 or 2 bits we can adjust the start/end of the conflicting ranges

        The ranges inside the container _must_ be sorted i.e. the range with lowest start must be at front

        :param container:
        :return:
        """
        partitions = []  # type: list[list[CommonRange]]
        # partition the container into overlapping partitions
        # results in something like [[A], [B,C], [D], [E,F,G]]] where B and C and E, F, G are overlapping
        for cur_rng in container:
            if len(partitions) == 0:
                partitions.append([cur_rng])
                continue

            last_rng = partitions[-1][-1]  # type: CommonRange
            if cur_rng.overlaps_with(last_rng):
                partitions[-1].append(cur_rng)
            else:
                partitions.append([cur_rng])

        # Todo: Adjust start/end of conflicting ranges if overlapping is very small (i.e. 1 or 2 bits)

        result = []
        # Go through these partitions and handle overlapping conflicts
        for partition in partitions:
            possible_solutions = []
            for i, rng in enumerate(partition):
                # Append every range to this solution that does not overlap with current rng
                solution = [rng] + [r for r in partition[i + 1:] if not rng.overlaps_with(r)]
                possible_solutions.append(solution)

            best_solution = max(possible_solutions, key=lambda sol: sum(r.score for r in sol))
            result.extend(best_solution)

        return CommonRangeContainer(result, message_indices=copy.copy(container.message_indices))

    @staticmethod
    def retransform_message_indices(common_ranges, message_type_indices: list, sync_ends) -> list:
        """
        Retransform the found message indices of an engine to the original index space
        based on the message indices of the message type.

        Furthermore, set the sync_end of the common ranges so bit_start and bit_end
        match the position in the original space

        :type common_ranges: list of CommonRange
        :param message_type_indices: Messages belonging to the message type the engine ran for
        :type sync_ends: np.ndarray
        :return:
        """
        result = []
        for common_range in common_ranges:
            # Retransform message indices into original space
            message_indices = np.fromiter((message_type_indices[i] for i in common_range.message_indices),
                                          dtype=int, count=len(common_range.message_indices))

            # If we have different sync_ends we need to create a new common range for each different sync_length
            matching_sync_ends = sync_ends[message_indices]
            for sync_end in np.unique(matching_sync_ends):
                rng = copy.deepcopy(common_range)
                rng.sync_end = sync_end
                rng.message_indices = set(message_indices[np.nonzero(matching_sync_ends == sync_end)])
                result.append(rng)

        return result

    @staticmethod
    def get_preamble_and_sync(preamble_starts, preamble_lengths, sync_ends, message_type_indices):
        """
        Get preamble and sync common ranges based on the data

        :type preamble_starts: np.ndarray
        :type preamble_lengths: np.ndarray
        :type sync_ends: np.ndarray
        :type message_type_indices: list
        :rtype: set of CommonRange
        """
        assert len(preamble_starts) == len(preamble_lengths) == len(sync_ends)

        result = set()  # type: set[CommonRange]
        for i in message_type_indices:
            preamble = CommonRange(preamble_starts[i], preamble_lengths[i], field_type="preamble", message_indices={i})
            existing_preamble = next((rng for rng in result if preamble == rng), None)
            if existing_preamble is not None:
                existing_preamble.message_indices.add(i)
            else:
                result.add(preamble)

            preamble_end = preamble_starts[i] + preamble_lengths[i]
            sync_end = sync_ends[i]
            sync = CommonRange(preamble_end, sync_end - preamble_end, field_type="synchronization", message_indices={i})
            existing_sync = next((rng for rng in result if sync == rng), None)
            if existing_sync is not None:
                existing_sync.message_indices.add(i)
            else:
                result.add(sync)

        return result
