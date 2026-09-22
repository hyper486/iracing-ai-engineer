// A process-local, single-entry cache of already validated descriptor metadata.
// Frame/layout/SessionInfo integrity checks are never replaced by this cache.
using System;
using System.Collections.Generic;
using System.Collections.ObjectModel;

namespace Aeis.CrewChiefReader
{
    internal sealed class ValidatedSchema
    {
        internal readonly ReadOnlyCollection<CVarHeader> Headers;
        internal readonly ReadOnlyCollection<ReadOnlyDictionary<string, object>> ProtocolDescriptors;

        internal ValidatedSchema(List<CVarHeader> headers)
        {
            Headers = headers.AsReadOnly();
            var protocol = new List<ReadOnlyDictionary<string, object>>();
            foreach (CVarHeader header in headers)
                protocol.Add(new ReadOnlyDictionary<string, object>(header.ToProtocol()));
            ProtocolDescriptors = protocol.AsReadOnly();
        }
    }

    internal sealed class ValidatedSchemaCache
    {
        private ValidatedSchema entry;
        private byte[] schema;
        private int version, variableCount, variableOffset, bufferCount, bufferLength;
        private int[] bufferOffsets;

        internal int EntryCount { get { return entry == null ? 0 : 1; } }

        internal ValidatedSchema GetOrBuild(SdkLayout layout)
        {
            if (Matches(layout)) return entry;
            // Drop the old entry before parsing a replacement, including invalid
            // replacements. Only one schema is ever retained, not a session log.
            Clear();
            var headers = CrewChiefSdkReader.GetVarHeaders(layout.Schema, layout.VariableCount, layout.BufferLength);
            var validated = new ValidatedSchema(headers);
            schema = (byte[])layout.Schema.Clone();
            version = layout.Version;
            variableCount = layout.VariableCount;
            variableOffset = layout.VariableOffset;
            bufferCount = layout.BufferCount;
            bufferLength = layout.BufferLength;
            bufferOffsets = (int[])layout.BufferOffsets.Clone();
            entry = validated;
            return entry;
        }

        private bool Matches(SdkLayout layout)
        {
            if (entry == null || version != layout.Version || variableCount != layout.VariableCount ||
                variableOffset != layout.VariableOffset || bufferCount != layout.BufferCount ||
                bufferLength != layout.BufferLength || !SdkLayout.SameBytes(schema, layout.Schema)) return false;
            for (int i = 0; i < bufferCount; i++)
                if (bufferOffsets[i] != layout.BufferOffsets[i]) return false;
            return true;
        }

        internal void Clear()
        {
            entry = null;
            schema = null;
            bufferOffsets = null;
        }
    }
}
