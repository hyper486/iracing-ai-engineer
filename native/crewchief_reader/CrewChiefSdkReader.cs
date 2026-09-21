// Read-only adaptation of Crew Chief iRSDKSharp/iRacingSDK.cs:
// GetVarHeaders, GetData and their descriptor constants/type dispatch.
// Copyright (c) 2019-2024 Britton IT Ltd. MIT; see LICENSE.CrewChief.
// All control APIs are omitted. See UPSTREAM.md for the exact source revision.
using System;
using System.Collections.Generic;
using System.Text;

namespace Aeis.CrewChiefReader
{
    internal static class CrewChiefSdkReader
    {
        internal const int VarHeaderSize = 144;
        internal const int MaxVariables = 4096;
        internal const int MaxArrayCount = 4096;
        internal const int MaxDecodedElements = 1048576;
        private const int VarOffsetOffset = 4;
        private const int VarCountOffset = 8;
        private const int VarNameOffset = 16;
        private const int VarDescOffset = 48;
        private const int VarUnitOffset = 112;
        private static readonly Encoding DescriptorEncoding = Encoding.GetEncoding(28591);

        // Crew Chief's table offsets and CVarHeader construction are retained,
        // but all reads now come from a frozen table, with validation before use.
        internal static List<CVarHeader> GetVarHeaders(byte[] schema, int variableCount, int bufferLength)
        {
            if (variableCount < 1 || variableCount > MaxVariables ||
                schema.Length != variableCount * VarHeaderSize)
                throw new SnapshotException("inconsistent", "invalid_descriptors");
            var headers = new List<CVarHeader>();
            var names = new HashSet<string>(StringComparer.Ordinal);
            var ranges = new List<long[]>();
            long totalElements = 0;
            for (int i = 0; i < variableCount; i++)
            {
                int at = i * VarHeaderSize;
                int type = BitConverter.ToInt32(schema, at);
                int offset = BitConverter.ToInt32(schema, at + VarOffsetOffset);
                int count = BitConverter.ToInt32(schema, at + VarCountOffset);
                string name = DecodeString(schema, at + VarNameOffset, 32);
                string desc = DecodeString(schema, at + VarDescOffset, 64);
                string unit = DecodeString(schema, at + VarUnitOffset, 32);
                var header = new CVarHeader(type, offset, count, name, desc, unit, schema[at + 12] != 0);
                totalElements += count;
                if (type < 0 || type > 5 || count < 1 || count > MaxArrayCount ||
                    offset < 0 || (long)offset + (long)header.Bytes * count > bufferLength ||
                    schema[at + 12] > 1 || String.IsNullOrEmpty(name) || name.IndexOf('\0') >= 0 ||
                    !names.Add(name) || totalElements > MaxDecodedElements)
                    throw new SnapshotException("inconsistent", "invalid_descriptors");
                headers.Add(header);
                ranges.Add(new long[] { offset, (long)offset + (long)header.Bytes * count });
            }
            ranges.Sort(delegate(long[] a, long[] b) { return a[0].CompareTo(b[0]); });
            for (int i = 1; i < ranges.Count; i++)
                if (ranges[i][0] < ranges[i - 1][1])
                    throw new SnapshotException("inconsistent", "overlapping_descriptors");
            return headers;
        }

        private static string DecodeString(byte[] bytes, int offset, int count)
        {
            return DescriptorEncoding.GetString(bytes, offset, count).TrimEnd('\0');
        }

        // Preserve the upstream scalar-vs-array and primitive-type dispatch;
        // unlike upstream, read a single frozen buffer, return unsigned bitfields
        // uniformly, and reject unsupported/missing values instead of returning 0.
        internal static object GetData(CVarHeader header, byte[] frozen)
        {
            int varOffset = header.Offset;
            int count = header.Count;
            if (varOffset < 0 || count < 1 || header.Bytes < 1 ||
                (long)varOffset + (long)header.Bytes * count > frozen.Length)
                throw new SnapshotException("inconsistent", "invalid_descriptor_range");
            if (header.Type == CVarHeader.VarType.irChar)
                return DecodeString(frozen, varOffset, count);
            if (header.Type == CVarHeader.VarType.irBool)
            {
                bool[] data = new bool[count];
                for (int i = 0; i < count; i++)
                {
                    if (frozen[varOffset + i] > 1) return null;
                    data[i] = frozen[varOffset + i] != 0;
                }
                return count > 1 ? (object)data : data[0];
            }
            if (header.Type == CVarHeader.VarType.irInt)
            {
                int[] data = new int[count];
                Buffer.BlockCopy(frozen, varOffset, data, 0, count * 4);
                return count > 1 ? (object)data : data[0];
            }
            if (header.Type == CVarHeader.VarType.irBitField)
            {
                uint[] data = new uint[count];
                Buffer.BlockCopy(frozen, varOffset, data, 0, count * 4);
                return count > 1 ? (object)data : data[0];
            }
            if (header.Type == CVarHeader.VarType.irFloat)
            {
                float[] data = new float[count];
                Buffer.BlockCopy(frozen, varOffset, data, 0, count * 4);
                double[] promoted = new double[count];
                for (int i = 0; i < count; i++)
                {
                    if (Single.IsNaN(data[i]) || Single.IsInfinity(data[i])) return null;
                    // Preserve the exact float32 value through JSON instead of
                    // JavaScriptSerializer's shortened Single representation.
                    promoted[i] = (double)data[i];
                }
                return count > 1 ? (object)promoted : promoted[0];
            }
            if (header.Type == CVarHeader.VarType.irDouble)
            {
                double[] data = new double[count];
                Buffer.BlockCopy(frozen, varOffset, data, 0, count * 8);
                for (int i = 0; i < count; i++)
                    if (Double.IsNaN(data[i]) || Double.IsInfinity(data[i])) return null;
                return count > 1 ? (object)data : data[0];
            }
            throw new SnapshotException("inconsistent", "unsupported_type");
        }
    }
}
