// Snapshot integrity and protocol wrapper written for iRacing AI Engineer.
// The descriptor decoder it calls is adapted from Crew Chief; see UPSTREAM.md.
using System;
using System.Collections.Generic;
using System.IO;
using System.IO.MemoryMappedFiles;

namespace Aeis.CrewChiefReader
{
    internal interface ISharedMemoryReader
    {
        long Capacity { get; }
        byte[] Read(long offset, int count);
    }

    // No caller-supplied name/path, write mapping, event mutation or control API.
    internal sealed class LiveSharedMemoryReader : ISharedMemoryReader, IDisposable
    {
        private const string MapName = "Local\\IRSDKMemMapFileName";
        private readonly MemoryMappedFile mapping;
        private readonly MemoryMappedViewAccessor view;

        internal LiveSharedMemoryReader()
        {
            mapping = MemoryMappedFile.OpenExisting(MapName, MemoryMappedFileRights.Read);
            try
            {
                view = mapping.CreateViewAccessor(0, 0, MemoryMappedFileAccess.Read);
            }
            catch
            {
                mapping.Dispose();
                throw;
            }
        }

        public long Capacity { get { return view.Capacity; } }

        public byte[] Read(long offset, int count)
        {
            if (offset < 0 || count < 0 || offset > Capacity - count)
                throw new SnapshotException("inconsistent", "invalid_read_range");
            byte[] result = new byte[count];
            if (view.ReadArray<byte>(offset, result, 0, count) != count)
                throw new SnapshotException("inconsistent", "short_read");
            return result;
        }

        public void Dispose()
        {
            try { view.Dispose(); }
            finally { mapping.Dispose(); }
        }
    }

    internal sealed class SnapshotException : Exception
    {
        internal string Status { get; private set; }
        internal string ErrorCode { get; private set; }

        internal SnapshotException(string status, string errorCode) : base(errorCode)
        {
            Status = status;
            ErrorCode = errorCode;
        }
    }

    internal sealed class SdkLayout
    {
        internal const int HeaderSize = 112;
        internal const int MaximumMappingBytes = 67108864;
        internal const int MaximumSectionBytes = 4194304;
        internal const int MaximumSessionBytes = 8388608;
        internal int Version, Status, TickRate, SessionUpdate, SessionLength, SessionOffset;
        internal int VariableCount, VariableOffset, BufferCount, BufferLength;
        internal int CurrentBuffer;
        internal int[] BufferTicks, BufferOffsets, BufferBegins;
        internal byte[] Schema;

        internal static SdkLayout Read(ISharedMemoryReader source)
        {
            if (!BitConverter.IsLittleEndian || source.Capacity < HeaderSize ||
                source.Capacity > MaximumMappingBytes)
                throw new SnapshotException("inconsistent", "invalid_mapping_size");
            byte[] raw = ReadExactly(source, 0, HeaderSize);
            var result = new SdkLayout
            {
                Version = BitConverter.ToInt32(raw, 0), Status = BitConverter.ToInt32(raw, 4),
                TickRate = BitConverter.ToInt32(raw, 8), SessionUpdate = BitConverter.ToInt32(raw, 12),
                SessionLength = BitConverter.ToInt32(raw, 16), SessionOffset = BitConverter.ToInt32(raw, 20),
                VariableCount = BitConverter.ToInt32(raw, 24), VariableOffset = BitConverter.ToInt32(raw, 28),
                BufferCount = BitConverter.ToInt32(raw, 32), BufferLength = BitConverter.ToInt32(raw, 36),
                CurrentBuffer = raw[44]
            };
            if (result.Version != 2 || result.TickRate < 1 || result.TickRate > 360 ||
                result.BufferCount < 1 || result.BufferCount > 4 ||
                result.VariableCount < 1 || result.VariableCount > CrewChiefSdkReader.MaxVariables ||
                result.BufferLength < 1 || result.BufferLength > MaximumSectionBytes ||
                result.SessionLength < 0 || result.SessionLength > MaximumSessionBytes ||
                result.SessionOffset < 0 || result.SessionOffset > source.Capacity ||
                result.CurrentBuffer >= result.BufferCount)
                throw new SnapshotException("inconsistent", "invalid_layout");
            if ((result.Status & 1) == 0)
                throw new SnapshotException("unavailable", "sdk_disconnected");
            if (result.SessionLength == 0)
                throw new SnapshotException("unavailable", "session_info_not_ready");

            result.BufferTicks = new int[result.BufferCount];
            result.BufferOffsets = new int[result.BufferCount];
            result.BufferBegins = new int[result.BufferCount];
            var ranges = new List<long[]> { new long[] { 0, HeaderSize } };
            ranges.Add(new long[] { result.VariableOffset,
                (long)result.VariableOffset + result.VariableCount * CrewChiefSdkReader.VarHeaderSize });
            ranges.Add(new long[] { result.SessionOffset, (long)result.SessionOffset + result.SessionLength });
            for (int i = 0; i < result.BufferCount; i++)
            {
                int at = 48 + i * 16;
                result.BufferTicks[i] = BitConverter.ToInt32(raw, at);
                result.BufferOffsets[i] = BitConverter.ToInt32(raw, at + 4);
                result.BufferBegins[i] = BitConverter.ToInt32(raw, at + 8);
                ranges.Add(new long[] { result.BufferOffsets[i], (long)result.BufferOffsets[i] + result.BufferLength });
            }
            ranges.Sort(delegate(long[] a, long[] b) { return a[0].CompareTo(b[0]); });
            long previousEnd = -1;
            foreach (long[] range in ranges)
            {
                if (range[0] < 0 || range[1] <= range[0] || range[1] > source.Capacity ||
                    range[0] < previousEnd)
                    throw new SnapshotException("inconsistent", "overlapping_or_invalid_sections");
                previousEnd = range[1];
            }
            result.Schema = ReadExactly(source, result.VariableOffset, result.VariableCount * CrewChiefSdkReader.VarHeaderSize);
            return result;
        }

        internal static byte[] ReadExactly(ISharedMemoryReader source, int offset, int count)
        {
            byte[] bytes = source.Read(offset, count);
            if (bytes == null || bytes.Length != count)
                throw new SnapshotException("inconsistent", "short_read");
            return bytes;
        }

        internal bool SameIdentity(SdkLayout other)
        {
            if (Version != other.Version || Status != other.Status || TickRate != other.TickRate ||
                SessionUpdate != other.SessionUpdate || SessionLength != other.SessionLength ||
                SessionOffset != other.SessionOffset || VariableCount != other.VariableCount ||
                VariableOffset != other.VariableOffset || BufferCount != other.BufferCount ||
                BufferLength != other.BufferLength || !SameBytes(Schema, other.Schema)) return false;
            for (int i = 0; i < BufferCount; i++)
                if (BufferOffsets[i] != other.BufferOffsets[i]) return false;
            return true;
        }

        internal static bool SameBytes(byte[] first, byte[] second)
        {
            if (first.Length != second.Length) return false;
            for (int i = 0; i < first.Length; i++) if (first[i] != second[i]) return false;
            return true;
        }
    }

    internal static class SnapshotReader
    {
        internal const string Protocol = "crewchief-readonly-v1";
        internal const int MaximumAttempts = 3;

        internal static Dictionary<string, object> Failure(string status, string code)
        {
            return new Dictionary<string, object>
            {
                { "protocol", Protocol }, { "status", status }, { "error_code", code }
            };
        }

        internal static Dictionary<string, object> Capture(ISharedMemoryReader source)
        {
            string lastCode = "snapshot_changed";
            for (int attempt = 0; attempt < MaximumAttempts; attempt++)
            {
                try { return CaptureOnce(source); }
                catch (SnapshotException error)
                {
                    if (error.Status == "unavailable") return Failure(error.Status, error.ErrorCode);
                    lastCode = error.ErrorCode;
                }
                catch (IOException) { return Failure("unavailable", "source_read_failed"); }
                catch (UnauthorizedAccessException) { return Failure("unavailable", "source_read_failed"); }
            }
            return Failure("inconsistent", lastCode);
        }

        private static Dictionary<string, object> CaptureOnce(ISharedMemoryReader source)
        {
            SdkLayout before = SdkLayout.Read(source);
            List<CVarHeader> headers = CrewChiefSdkReader.GetVarHeaders(before.Schema, before.VariableCount, before.BufferLength);
            int selected = -1;
            for (int i = 0; i < before.BufferCount; i++)
                if (before.BufferBegins[i] == before.BufferTicks[i] &&
                    (selected < 0 || before.BufferTicks[i] > before.BufferTicks[selected])) selected = i;
            if (selected < 0) throw new SnapshotException("inconsistent", "buffer_write_in_progress");

            byte[] sessionFirst = SdkLayout.ReadExactly(source, before.SessionOffset, before.SessionLength);
            byte[] frozen = SdkLayout.ReadExactly(source, before.BufferOffsets[selected], before.BufferLength);
            SdkLayout between = SdkLayout.Read(source);
            RequireStable(before, between, selected);
            byte[] sessionSecond = SdkLayout.ReadExactly(source, between.SessionOffset, between.SessionLength);
            SdkLayout after = SdkLayout.Read(source);
            RequireStable(before, after, selected);
            if (!SdkLayout.SameBytes(sessionFirst, sessionSecond))
                throw new SnapshotException("inconsistent", "session_info_changed");

            var values = new Dictionary<string, object>(StringComparer.Ordinal);
            var descriptors = new List<Dictionary<string, object>>();
            var errors = new List<string>();
            foreach (CVarHeader header in headers)
            {
                descriptors.Add(header.ToProtocol());
                object value = CrewChiefSdkReader.GetData(header, frozen);
                values.Add(header.Name, value);
                if (value == null) errors.Add(header.Name);
            }
            return new Dictionary<string, object>
            {
                { "protocol", Protocol }, { "status", "ok" },
                { "connection", new Dictionary<string, object>
                    {
                        { "header_version", before.Version }, { "raw_header_status", before.Status },
                        { "tick_rate_hz", before.TickRate }, { "variable_count", before.VariableCount },
                        { "buffer_count", before.BufferCount }, { "buffer_len", before.BufferLength }
                    }
                },
                { "buffer_tick", before.BufferTicks[selected] },
                { "session_info_update", before.SessionUpdate },
                { "session_info_b64", Convert.ToBase64String(sessionFirst) },
                { "descriptors", descriptors }, { "values", values }, { "read_errors", errors }
            };
        }

        private static void RequireStable(SdkLayout before, SdkLayout after, int selected)
        {
            if (!before.SameIdentity(after) ||
                before.BufferTicks[selected] != after.BufferTicks[selected] ||
                before.BufferTicks[selected] != after.BufferBegins[selected])
                throw new SnapshotException("inconsistent", "snapshot_changed");
        }
    }
}
