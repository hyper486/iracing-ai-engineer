// Synthetic byte-array tests only. Never compiled into the production entry point.
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Text;
using System.Web.Script.Serialization;

namespace Aeis.CrewChiefReader.Tests
{
    internal sealed class MemorySource : ISharedMemoryReader
    {
        internal readonly byte[] Bytes;
        internal Action<MemorySource, long, int> AfterRead;
        internal Func<long, int, byte[]> OverrideRead;
        internal int HeaderReads;

        internal MemorySource(byte[] bytes) { Bytes = bytes; }
        public long Capacity { get { return Bytes.Length; } }

        public byte[] Read(long offset, int count)
        {
            if (offset == 0) HeaderReads++;
            if (OverrideRead != null) return OverrideRead(offset, count);
            var copy = new byte[count];
            Array.Copy(Bytes, offset, copy, 0, count);
            if (AfterRead != null) AfterRead(this, offset, count);
            return copy;
        }
    }

    internal sealed class Fixture
    {
        internal static bool UseWarmCache;
        internal const int SchemaOffset = 128;
        internal const int BufferLength = 256;
        internal readonly MemorySource Source;
        internal readonly ValidatedSchemaCache Cache;
        internal readonly int SessionOffset;
        internal readonly int[] BufferOffsets = new int[3];
        internal readonly Dictionary<string, int> FieldOffsets = new Dictionary<string, int>();
        internal readonly Dictionary<string, int> DescriptorOffsets = new Dictionary<string, int>();

        internal Fixture()
        {
            string[] names = { "SessionTime", "SessionTick", "SessionNum", "IsReplayPlaying", "Speed",
                "Throttle", "Brake", "Clutch", "SteeringWheelAngle", "FuelLevel", "Lap", "LapDistPct",
                "IsOnTrack", "OnPitRoad", "PlayerCarIdx", "CarIdxLapDistPct", "CarIdxOnPitRoad",
                "CarIdxLap", "SessionFlags", "VehicleName", "DoubleArray" };
            int[] types = { 5, 2, 2, 1, 4, 4, 4, 4, 4, 4, 2, 4, 1, 1, 2, 4, 1, 2, 3, 0, 5 };
            int[] counts = { 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 3, 3, 3, 1, 16, 2 };
            SessionOffset = SchemaOffset + names.Length * CrewChiefSdkReader.VarHeaderSize;
            for (int i = 0; i < 3; i++) BufferOffsets[i] = SessionOffset + 512 + i * BufferLength;
            Source = new MemorySource(new byte[BufferOffsets[2] + BufferLength]);
            Put(0, 2); Put(4, 1); Put(8, 60); Put(12, 7); Put(16, 512); Put(20, SessionOffset);
            Put(24, names.Length); Put(28, SchemaOffset); Put(32, 3); Put(36, BufferLength);
            Source.Bytes[44] = 1;
            int[] ticks = { 10, 30, 20 };
            for (int i = 0; i < 3; i++)
            {
                Put(48 + i * 16, ticks[i]); Put(52 + i * 16, BufferOffsets[i]); Put(56 + i * 16, ticks[i]);
            }
            string session = "---\nWeekendInfo:\n SimMode: full\n TrackName: synthetic_test\n TrackLength: 5.000 km\n" +
                " SessionID: 0\n SubSessionID: 0\nDriverInfo:\n DriverCarIdx: 0\n Drivers:\n - CarIdx: 0\n" +
                "   CarScreenName: SyntheticCar\n   CarClassID: 1\nSessionInfo:\n Sessions:\n - SessionNum: 0\n" +
                "   SessionType: Practice\n";
            WriteBytes(SessionOffset, Encoding.UTF8.GetBytes(session));
            int offset = 0;
            for (int i = 0; i < names.Length; i++)
            {
                int at = SchemaOffset + i * CrewChiefSdkReader.VarHeaderSize;
                FieldOffsets[names[i]] = offset;
                DescriptorOffsets[names[i]] = at;
                Put(at, types[i]); Put(at + 4, offset); Put(at + 8, counts[i]);
                WriteBytes(at + 16, Encoding.ASCII.GetBytes(names[i]));
                WriteBytes(at + 48, Encoding.ASCII.GetBytes("Synthetic test descriptor"));
                if (names[i] == "Speed") WriteBytes(at + 112, Encoding.ASCII.GetBytes("m/s"));
                if (names[i] == "CarIdxLapDistPct") Source.Bytes[at + 12] = 1;
                int width = types[i] <= 1 ? 1 : (types[i] == 5 ? 8 : 4);
                offset += width * counts[i];
            }
            for (int i = 0; i < 3; i++)
            {
                Set(i, "SessionTime", BitConverter.GetBytes(12.5));
                Set(i, "SessionTick", BitConverter.GetBytes(ticks[i]));
                Set(i, "SessionNum", BitConverter.GetBytes(0));
                Set(i, "Speed", BitConverter.GetBytes(45.25f));
                Set(i, "Throttle", BitConverter.GetBytes(0.1f));
                Set(i, "SteeringWheelAngle", BitConverter.GetBytes(0.25f));
                Set(i, "FuelLevel", BitConverter.GetBytes(42.5f));
                Set(i, "Lap", BitConverter.GetBytes(2));
                Set(i, "LapDistPct", BitConverter.GetBytes(0.25f));
                Set(i, "IsOnTrack", new byte[] { 1 });
                Set(i, "CarIdxLapDistPct", new byte[12]);
                Set(i, "CarIdxOnPitRoad", new byte[] { 0, 1, 0 });
                Set(i, "CarIdxLap", new byte[12]);
                Set(i, "SessionFlags", BitConverter.GetBytes(UInt32.MaxValue));
                Set(i, "VehicleName", Encoding.GetEncoding(28591).GetBytes("Synthetic Caf\u00e9"));
                Set(i, "DoubleArray", BitConverter.GetBytes(0.1));
            }
            if (UseWarmCache)
            {
                Cache = new ValidatedSchemaCache();
                SnapshotReader.Capture(Source, Cache);
                Source.HeaderReads = 0;
            }
        }

        internal void Put(int at, int value) { WriteBytes(at, BitConverter.GetBytes(value)); }
        internal void WriteBytes(int at, byte[] value) { Array.Copy(value, 0, Source.Bytes, at, value.Length); }
        internal void Set(int buffer, string name, byte[] value) { WriteBytes(BufferOffsets[buffer] + FieldOffsets[name], value); }
        internal Dictionary<string, object> Capture() { return SnapshotReader.Capture(Source, Cache); }
    }

    internal static class ReaderTests
    {
        private static int passed;

        private static int Main(string[] args)
        {
            Console.OutputEncoding = new UTF8Encoding(false);
            if (args.Length == 1 && args[0] == "--emit-snapshot")
            {
                Console.Out.WriteLine(Program.SerializeResponse(new Fixture().Capture()));
                return 0;
            }
            if (args.Length == 1 && args[0] == "--emit-cached-snapshots")
            {
                var fixture = new Fixture();
                var cache = new ValidatedSchemaCache();
                Console.Out.WriteLine(Program.SerializeResponse(SnapshotReader.Capture(fixture.Source)));
                Console.Out.WriteLine(Program.SerializeResponse(SnapshotReader.Capture(fixture.Source, cache)));
                Console.Out.WriteLine(Program.SerializeResponse(SnapshotReader.Capture(fixture.Source, cache)));
                return 0;
            }
            if (args.Length == 1 && args[0] == "--benchmark-cache")
            {
                BenchmarkCache();
                return 0;
            }
            if (args.Length != 0) return 2;
            Run("nonmonotonic ticks choose newest complete buffer", TestValid);
            Run("in-progress newest buffer is not selected", TestIncompleteNewest);
            Run("all buffers in progress fail bounded", TestAllIncomplete);
            Run("selected tick changes fail bounded", TestTickChanges);
            Run("begin marker changes reject torn copy", TestBeginChanges);
            Run("unselected buffer advances without false rejection", TestOtherBufferAdvances);
            Run("session bytes change without counter fails", TestSessionBytesChange);
            Run("session counter changes fail", TestSessionCounterChange);
            Run("descriptor schema changes fail", TestSchemaChanges);
            Run("buffer offsets change fail", TestBufferOffsetChanges);
            Run("retry succeeds after a single observed race", TestOneRaceRecovers);
            Run("invalid layout versions counts and indices", TestInvalidLayouts);
            Run("overlapping and out of bounds sections", TestInvalidSections);
            Run("invalid variable descriptors and duplicate names", TestInvalidDescriptors);
            Run("overlapping variable spans rejected", TestOverlappingDescriptors);
            Run("mapping and section resource caps", TestResourceCaps);
            Run("short reads rejected", TestShortRead);
            Run("disconnect and unready session unavailable", TestUnavailable);
            Run("float32 precision survives real JSON serializer", TestFloatPrecision);
            Run("nonfinite scalars and arrays are null plus errors", TestInvalidValues);
            Run("frozen frame is independent of backing memory", TestFrozenBuffer);
            Run("exception messages are not serialized", TestErrorPrivacy);
            Run("UTF8 byte output limit is enforced", TestOutputLimit);
            Run("bounded requests CRLF EOF and continuation", TestRequests);
            Run("cached and uncached protocol bytes are identical", TestCacheEquivalence);
            Run("same-counter schema content changes rebuild cache", TestCacheSchemaChanges);
            Run("all schema layout key facts invalidate reuse", TestCacheLayoutKey);
            Run("schema cache retains only one immutable entry", TestCacheBoundedAndImmutable);
            Run("failed snapshots clear previously warmed cache", TestCacheFailures);
            Run("warm cache retains all per-frame safety reads", TestCacheSafetyReads);
            Run("warm metadata never caches values or session bytes", TestCacheFreshValuesAndSession);
            Run("shared serializer recovers and handles concurrency", TestSerializerReuse);
            Console.Out.WriteLine("PASS " + passed + " native synthetic test groups");
            return 0;
        }

        private static void Run(string name, Action test)
        {
            try
            {
                // Run every existing torn-read/bounds/value test both uncached
                // and with the fixture's schema cache populated before mutation.
                Fixture.UseWarmCache = false;
                test(); passed++;
                Fixture.UseWarmCache = true;
                test(); passed++;
                Fixture.UseWarmCache = false;
            }
            catch (Exception error)
            {
                Console.Error.WriteLine("FAIL " + name + ": " + error.GetType().Name);
                Environment.Exit(1);
            }
        }

        private static void Check(bool condition) { if (!condition) throw new InvalidOperationException("assertion_failed"); }
        private static void Equal(object expected, object actual) { Check(Object.Equals(expected, actual)); }
        private static Dictionary<string, object> Values(Dictionary<string, object> snapshot)
        {
            Equal("ok", snapshot["status"]);
            return (Dictionary<string, object>)snapshot["values"];
        }

        private static void TestValid()
        {
            var fixture = new Fixture();
            var snapshot = fixture.Capture();
            var values = Values(snapshot);
            Equal(SnapshotReader.Protocol, snapshot["protocol"]);
            Equal(30, snapshot["buffer_tick"]);
            Equal(30, values["SessionTick"]);
            Equal(UInt32.MaxValue, values["SessionFlags"]);
            Equal(false, values["IsReplayPlaying"]);
            Equal("Synthetic Caf\u00e9", values["VehicleName"]);
            Equal(3, ((double[])values["CarIdxLapDistPct"]).Length);
            Equal(true, ((bool[])values["CarIdxOnPitRoad"])[1]);
            Equal(3, ((int[])values["CarIdxLap"]).Length);
            Equal(2, ((double[])values["DoubleArray"]).Length);
            var descriptors = (System.Collections.IList)snapshot["descriptors"];
            Equal(values.Count, descriptors.Count);
            bool timeCountFound = false;
            foreach (IDictionary<string, object> item in descriptors)
                if ((string)item["name"] == "CarIdxLapDistPct") timeCountFound = item["count_as_time"].Equals(true);
            Check(timeCountFound);
            byte[] session = Convert.FromBase64String((string)snapshot["session_info_b64"]);
            Equal(512, session.Length);
            Equal(0, ((List<string>)snapshot["read_errors"]).Count);
        }

        private static void TestIncompleteNewest()
        {
            var fixture = new Fixture();
            fixture.Put(72, 31);
            Equal(20, fixture.Capture()["buffer_tick"]);
        }

        private static void TestAllIncomplete()
        {
            var fixture = new Fixture();
            for (int i = 0; i < 3; i++) fixture.Put(56 + i * 16, 99);
            Equal("buffer_write_in_progress", fixture.Capture()["error_code"]);
            Equal(3, fixture.Source.HeaderReads);
        }

        private static void TestTickChanges()
        {
            var fixture = new Fixture();
            int tick = 30;
            fixture.Source.AfterRead = delegate(MemorySource source, long at, int count)
            {
                if (at == fixture.BufferOffsets[1]) { tick++; fixture.Put(64, tick); fixture.Put(72, tick); }
            };
            Equal("snapshot_changed", fixture.Capture()["error_code"]);
            Equal(6, fixture.Source.HeaderReads);
        }

        private static void TestBeginChanges()
        {
            var fixture = new Fixture();
            fixture.Source.AfterRead = delegate(MemorySource source, long at, int count)
            {
                for (int i = 0; i < 3; i++)
                    if (at == fixture.BufferOffsets[i]) fixture.Put(56 + i * 16, 99);
            };
            Equal("inconsistent", fixture.Capture()["status"]);
        }

        private static void TestOtherBufferAdvances()
        {
            var fixture = new Fixture();
            fixture.Source.AfterRead = delegate(MemorySource source, long at, int count)
            {
                if (at == fixture.BufferOffsets[1])
                {
                    fixture.Put(48, 31); fixture.Put(56, 31); source.Bytes[44] = 0;
                }
            };
            var snapshot = fixture.Capture();
            Equal("ok", snapshot["status"]);
            Equal(30, snapshot["buffer_tick"]);
        }

        private static void TestSessionBytesChange()
        {
            var fixture = new Fixture();
            fixture.Source.AfterRead = delegate(MemorySource source, long at, int count)
            {
                if (at == fixture.SessionOffset) source.Bytes[fixture.SessionOffset] ^= 1;
            };
            Equal("session_info_changed", fixture.Capture()["error_code"]);
            Equal(9, fixture.Source.HeaderReads);
        }

        private static void TestSessionCounterChange()
        {
            var fixture = new Fixture();
            int update = 7;
            fixture.Source.AfterRead = delegate(MemorySource source, long at, int count)
            {
                if (at == fixture.SessionOffset) fixture.Put(12, ++update);
            };
            Equal("snapshot_changed", fixture.Capture()["error_code"]);
        }

        private static void TestSchemaChanges()
        {
            var fixture = new Fixture();
            fixture.Source.AfterRead = delegate(MemorySource source, long at, int count)
            {
                if (at == Fixture.SchemaOffset) source.Bytes[Fixture.SchemaOffset + 48] ^= 1;
            };
            Equal("snapshot_changed", fixture.Capture()["error_code"]);
        }

        private static void TestBufferOffsetChanges()
        {
            var fixture = new Fixture();
            fixture.Source.AfterRead = delegate(MemorySource source, long at, int count)
            {
                if (at == fixture.BufferOffsets[1]) fixture.Put(68, 0);
            };
            Equal("inconsistent", fixture.Capture()["status"]);
        }

        private static void TestOneRaceRecovers()
        {
            var fixture = new Fixture();
            fixture.Source.AfterRead = delegate(MemorySource source, long at, int count)
            {
                if (at == fixture.BufferOffsets[1])
                {
                    fixture.Put(64, 31); fixture.Put(72, 31);
                    fixture.Set(1, "SessionTick", BitConverter.GetBytes(31));
                    source.AfterRead = null;
                }
            };
            Equal(31, fixture.Capture()["buffer_tick"]);
            Equal(5, fixture.Source.HeaderReads);
        }

        private static void TestInvalidLayouts()
        {
            int[,] cases = { { 0, 1 }, { 8, 0 }, { 8, 361 }, { 24, 0 }, { 24, 4097 },
                { 32, 0 }, { 32, 5 }, { 36, 0 }, { 16, -1 }, { 20, -1 } };
            for (int i = 0; i < cases.GetLength(0); i++)
            {
                var fixture = new Fixture();
                fixture.Put(cases[i, 0], cases[i, 1]);
                Equal("inconsistent", fixture.Capture()["status"]);
            }
            var badIndex = new Fixture();
            badIndex.Source.Bytes[44] = 3;
            Equal("invalid_layout", badIndex.Capture()["error_code"]);
        }

        private static void TestInvalidSections()
        {
            int[,] cases = { { 28, 0 }, { 28, Int32.MaxValue }, { 20, 0 }, { 52, 128 },
                { 52, -1 }, { 52, Int32.MaxValue } };
            for (int i = 0; i < cases.GetLength(0); i++)
            {
                var fixture = new Fixture();
                fixture.Put(cases[i, 0], cases[i, 1]);
                Equal("inconsistent", fixture.Capture()["status"]);
            }
        }

        private static void TestInvalidDescriptors()
        {
            int[,] cases = { { 0, 6 }, { 0, -1 }, { 4, -1 }, { 4, 255 }, { 8, 0 },
                { 8, 4097 }, { 8, Int32.MaxValue }, { 12, 2 } };
            for (int i = 0; i < cases.GetLength(0); i++)
            {
                var fixture = new Fixture();
                fixture.Put(Fixture.SchemaOffset + cases[i, 0], cases[i, 1]);
                Equal("invalid_descriptors", fixture.Capture()["error_code"]);
            }
            var duplicate = new Fixture();
            Array.Copy(duplicate.Source.Bytes, Fixture.SchemaOffset + 16, duplicate.Source.Bytes,
                Fixture.SchemaOffset + 144 + 16, 32);
            Equal("invalid_descriptors", duplicate.Capture()["error_code"]);
            var empty = new Fixture();
            Array.Clear(empty.Source.Bytes, Fixture.SchemaOffset + 16, 32);
            Equal("invalid_descriptors", empty.Capture()["error_code"]);
        }

        private static void TestOverlappingDescriptors()
        {
            var fixture = new Fixture();
            fixture.Put(Fixture.SchemaOffset + 144 + 4, 0);
            Equal("overlapping_descriptors", fixture.Capture()["error_code"]);
        }

        private static void TestResourceCaps()
        {
            var tiny = new MemorySource(new byte[100]);
            Equal("invalid_mapping_size", SnapshotReader.Capture(tiny)["error_code"]);
            var fixture = new Fixture();
            fixture.Put(36, SdkLayout.MaximumSectionBytes + 1);
            Equal("invalid_layout", fixture.Capture()["error_code"]);
            fixture = new Fixture();
            fixture.Put(16, SdkLayout.MaximumSessionBytes + 1);
            Equal("invalid_layout", fixture.Capture()["error_code"]);
        }

        private static void TestShortRead()
        {
            var fixture = new Fixture();
            fixture.Source.OverrideRead = delegate(long at, int count) { return new byte[count - 1]; };
            Equal("short_read", fixture.Capture()["error_code"]);
            Equal(3, fixture.Source.HeaderReads);
        }

        private static void TestUnavailable()
        {
            var fixture = new Fixture();
            fixture.Put(4, 0);
            Equal("sdk_disconnected", fixture.Capture()["error_code"]);
            Equal(1, fixture.Source.HeaderReads);
            fixture = new Fixture();
            fixture.Put(16, 0);
            Equal("session_info_not_ready", fixture.Capture()["error_code"]);
        }

        private static void TestFloatPrecision()
        {
            var fixture = new Fixture();
            fixture.Set(1, "CarIdxLapDistPct", BitConverter.GetBytes(0.1f));
            var serializer = new JavaScriptSerializer();
            var json = serializer.Deserialize<Dictionary<string, object>>(Program.SerializeResponse(fixture.Capture()));
            var values = (Dictionary<string, object>)json["values"];
            Equal((double)0.1f, Convert.ToDouble(values["Throttle"]));
            Equal((double)0.1f, Convert.ToDouble(((System.Collections.ArrayList)values["CarIdxLapDistPct"])[0]));
        }

        private static void TestInvalidValues()
        {
            var fixture = new Fixture();
            fixture.Set(1, "Throttle", BitConverter.GetBytes(Single.NaN));
            fixture.Set(1, "CarIdxLapDistPct", BitConverter.GetBytes(Single.PositiveInfinity));
            fixture.Set(1, "SessionTime", BitConverter.GetBytes(Double.NegativeInfinity));
            fixture.Set(1, "DoubleArray", BitConverter.GetBytes(Double.NaN));
            fixture.Set(1, "IsReplayPlaying", new byte[] { 2 });
            var snapshot = fixture.Capture();
            var values = Values(snapshot);
            var errors = (List<string>)snapshot["read_errors"];
            Equal(5, errors.Count);
            foreach (string name in errors) Equal(null, values[name]);
            string json = Program.SerializeResponse(snapshot);
            Check(json.IndexOf("NaN", StringComparison.Ordinal) < 0);
            Check(json.IndexOf("Infinity", StringComparison.Ordinal) < 0);
        }

        private static void TestFrozenBuffer()
        {
            var fixture = new Fixture();
            fixture.Source.AfterRead = delegate(MemorySource source, long at, int count)
            {
                if (at == fixture.BufferOffsets[1]) fixture.Set(1, "FuelLevel", BitConverter.GetBytes(0.0f));
            };
            // Even a writer violating tick markers cannot mutate our frozen copy.
            Equal(42.5, Values(fixture.Capture())["FuelLevel"]);
        }

        private static void TestErrorPrivacy()
        {
            var fixture = new Fixture();
            fixture.Source.OverrideRead = delegate(long at, int count) { throw new IOException("synthetic_private_sentinel"); };
            string json = Program.SerializeResponse(fixture.Capture());
            Check(json.Contains("source_read_failed"));
            Check(!json.Contains("synthetic_private_sentinel"));
        }

        private static void TestOutputLimit()
        {
            var response = new Dictionary<string, object> { { "value", new string('\u4e2d', 6000000) } };
            string json = Program.SerializeResponse(response);
            Check(json.Contains("output_limit"));
            Check(Encoding.UTF8.GetByteCount(json) + 2 < Program.MaximumOutputBytes);
            response["value"] = new string('a', Program.MaximumOutputBytes);
            Check(Program.SerializeResponse(response).Contains("output_limit"));
        }

        private static void TestRequests()
        {
            bool tooLong;
            var input = new StringReader("snapshot\r\n" + new string('x', 1000) + "\nsnapshot");
            Equal("snapshot", Program.ReadRequest(input, out tooLong));
            Check(!tooLong);
            Equal(64, Program.ReadRequest(input, out tooLong).Length);
            Check(tooLong);
            Equal("snapshot", Program.ReadRequest(input, out tooLong));
            Check(!tooLong);
            Equal(null, Program.ReadRequest(input, out tooLong));
        }

        private static void TestCacheEquivalence()
        {
            var fixture = new Fixture();
            var cache = new ValidatedSchemaCache();
            string uncached = Program.SerializeResponse(SnapshotReader.Capture(fixture.Source));
            var first = SnapshotReader.Capture(fixture.Source, cache);
            var second = SnapshotReader.Capture(fixture.Source, cache);
            Equal(uncached, Program.SerializeResponse(first));
            Equal(uncached, Program.SerializeResponse(second));
            Check(Object.ReferenceEquals(first["descriptors"], second["descriptors"]));
            Check(!Object.ReferenceEquals(first["values"], second["values"]));
        }

        private static void TestCacheSchemaChanges()
        {
            var fixture = new Fixture();
            var cache = new ValidatedSchemaCache();
            var first = SnapshotReader.Capture(fixture.Source, cache);
            fixture.Source.Bytes[Fixture.SchemaOffset + 48] = (byte)'X';
            var changed = SnapshotReader.Capture(fixture.Source, cache);
            Equal("ok", changed["status"]);
            Equal(first["session_info_update"], changed["session_info_update"]);
            Check(!Object.ReferenceEquals(first["descriptors"], changed["descriptors"]));
            Equal(Program.SerializeResponse(SnapshotReader.Capture(fixture.Source)), Program.SerializeResponse(changed));
            fixture.Put(Fixture.SchemaOffset, 6);
            Equal("invalid_descriptors", SnapshotReader.Capture(fixture.Source, cache)["error_code"]);
            Equal(0, cache.EntryCount);
        }

        private static void TestCacheLayoutKey()
        {
            var fixture = new Fixture();
            Action<SdkLayout>[] changes = {
                delegate(SdkLayout layout) { layout.Version++; },
                delegate(SdkLayout layout) { layout.VariableOffset++; },
                delegate(SdkLayout layout) { layout.BufferLength++; },
                delegate(SdkLayout layout) { layout.BufferCount--; },
                delegate(SdkLayout layout) { layout.BufferOffsets[0]++; }
            };
            foreach (Action<SdkLayout> change in changes)
            {
                var cache = new ValidatedSchemaCache();
                SdkLayout original = SdkLayout.Read(fixture.Source);
                var first = cache.GetOrBuild(original);
                SdkLayout replacement = SdkLayout.Read(fixture.Source);
                change(replacement);
                Check(!Object.ReferenceEquals(first, cache.GetOrBuild(replacement)));
                Equal(1, cache.EntryCount);
            }
            var invalidCountCache = new ValidatedSchemaCache();
            SdkLayout invalidCount = SdkLayout.Read(fixture.Source);
            invalidCountCache.GetOrBuild(invalidCount);
            invalidCount.VariableCount--;
            bool rejected = false;
            try { invalidCountCache.GetOrBuild(invalidCount); }
            catch (SnapshotException) { rejected = true; }
            Check(rejected);
            Equal(0, invalidCountCache.EntryCount);
        }

        private static void TestCacheBoundedAndImmutable()
        {
            var fixture = new Fixture();
            var cache = new ValidatedSchemaCache();
            for (int i = 0; i < 20; i++)
            {
                fixture.Source.Bytes[Fixture.SchemaOffset + 48] = (byte)('A' + i);
                Equal("ok", SnapshotReader.Capture(fixture.Source, cache)["status"]);
                Equal(1, cache.EntryCount);
            }
            var snapshot = SnapshotReader.Capture(fixture.Source, cache);
            var descriptors = (System.Collections.IList)snapshot["descriptors"];
            bool blockedList = false, blockedEntry = false;
            try { descriptors.Clear(); }
            catch (NotSupportedException) { blockedList = true; }
            try { ((IDictionary<string, object>)descriptors[0])["count"] = 999; }
            catch (NotSupportedException) { blockedEntry = true; }
            Check(blockedList && blockedEntry);
            Equal(Program.SerializeResponse(snapshot), Program.SerializeResponse(SnapshotReader.Capture(fixture.Source, cache)));
            cache.Clear();
            Equal(0, cache.EntryCount);
        }

        private static void TestCacheFailures()
        {
            Action<Fixture>[] failures = {
                delegate(Fixture fixture) { fixture.Put(4, 0); },
                delegate(Fixture fixture) { fixture.Put(16, 0); },
                delegate(Fixture fixture) { fixture.Put(Fixture.SchemaOffset + 8, 0); },
                delegate(Fixture fixture) {
                    fixture.Source.OverrideRead = delegate(long at, int count) { throw new IOException("synthetic_failure"); };
                },
                delegate(Fixture fixture) {
                    fixture.Source.AfterRead = delegate(MemorySource source, long at, int count) {
                        if (at == fixture.SessionOffset) source.Bytes[fixture.SessionOffset] ^= 1;
                    };
                }
            };
            foreach (Action<Fixture> failure in failures)
            {
                var fixture = new Fixture();
                var cache = new ValidatedSchemaCache();
                var before = SnapshotReader.Capture(fixture.Source, cache);
                failure(fixture);
                Check((string)SnapshotReader.Capture(fixture.Source, cache)["status"] != "ok");
                Equal(0, cache.EntryCount);
                var recovered = SnapshotReader.Capture(new Fixture().Source, cache);
                Equal("ok", recovered["status"]);
                Check(!Object.ReferenceEquals(before["descriptors"], recovered["descriptors"]));
            }
        }

        private static void TestCacheSafetyReads()
        {
            var fixture = new Fixture();
            var cache = new ValidatedSchemaCache();
            SnapshotReader.Capture(fixture.Source, cache);
            int headers = 0, schemas = 0, sessions = 0, buffers = 0;
            fixture.Source.AfterRead = delegate(MemorySource source, long at, int count)
            {
                if (at == 0) headers++;
                if (at == Fixture.SchemaOffset) schemas++;
                if (at == fixture.SessionOffset) sessions++;
                if (at == fixture.BufferOffsets[1]) buffers++;
            };
            Equal("ok", SnapshotReader.Capture(fixture.Source, cache)["status"]);
            Equal(3, headers); Equal(3, schemas); Equal(2, sessions); Equal(1, buffers);
        }

        private static void TestCacheFreshValuesAndSession()
        {
            var fixture = new Fixture();
            var cache = new ValidatedSchemaCache();
            var first = SnapshotReader.Capture(fixture.Source, cache);
            fixture.Set(1, "FuelLevel", BitConverter.GetBytes(41.0f));
            fixture.Source.Bytes[fixture.SessionOffset + 3] = (byte)'\r';
            var changed = SnapshotReader.Capture(fixture.Source, cache);
            Equal(41.0, Values(changed)["FuelLevel"]);
            Check(!Object.Equals(first["session_info_b64"], changed["session_info_b64"]));
            Equal(first["session_info_update"], changed["session_info_update"]);
            Check(Object.ReferenceEquals(first["descriptors"], changed["descriptors"]));
            fixture.Set(1, "FuelLevel", BitConverter.GetBytes(Single.NaN));
            var invalid = SnapshotReader.Capture(fixture.Source, cache);
            Check(((List<string>)invalid["read_errors"]).Contains("FuelLevel"));
            fixture.Set(1, "FuelLevel", BitConverter.GetBytes(40.0f));
            var recovered = SnapshotReader.Capture(fixture.Source, cache);
            Equal(40.0, Values(recovered)["FuelLevel"]);
            Equal(0, ((List<string>)recovered["read_errors"]).Count);
        }

        private static void TestSerializerReuse()
        {
            var packet = new Fixture().Capture();
            string expected = Program.SerializeResponse(packet);
            var oversized = new Dictionary<string, object> { { "text", new string('a', Program.MaximumOutputBytes) } };
            Check(Program.SerializeResponse(oversized).Contains("output_limit"));
            Equal(expected, Program.SerializeResponse(packet));
            System.Threading.Tasks.Parallel.For(0, 20, delegate(int index)
            {
                Equal(expected, Program.SerializeResponse(packet));
            });
        }

        private static MemorySource BenchmarkSource()
        {
            const int variableCount = 335;
            int sessionOffset = Fixture.SchemaOffset + variableCount * CrewChiefSdkReader.VarHeaderSize;
            const int sessionLength = 16384;
            int bufferOffset = sessionOffset + sessionLength;
            const int bufferLength = variableCount * 4;
            var source = new MemorySource(new byte[bufferOffset + bufferLength]);
            Action<int, int> put = delegate(int at, int value) {
                Array.Copy(BitConverter.GetBytes(value), 0, source.Bytes, at, 4);
            };
            put(0, 2); put(4, 1); put(8, 60); put(12, 7); put(16, sessionLength);
            put(20, sessionOffset); put(24, variableCount); put(28, Fixture.SchemaOffset);
            put(32, 1); put(36, bufferLength); put(48, 30); put(52, bufferOffset); put(56, 30);
            byte[] session = Encoding.ASCII.GetBytes("---\nWeekendInfo:\n SimMode: full\n TrackName: synthetic_benchmark\n");
            Array.Copy(session, 0, source.Bytes, sessionOffset, session.Length);
            for (int i = 0; i < variableCount; i++)
            {
                int descriptor = Fixture.SchemaOffset + i * CrewChiefSdkReader.VarHeaderSize;
                put(descriptor, 4); put(descriptor + 4, i * 4); put(descriptor + 8, 1);
                byte[] name = Encoding.ASCII.GetBytes("SyntheticMetric" + i);
                Array.Copy(name, 0, source.Bytes, descriptor + 16, name.Length);
                Array.Copy(BitConverter.GetBytes(0.1f + i), 0, source.Bytes, bufferOffset + i * 4, 4);
            }
            return source;
        }

        private static double Measure(MemorySource source, ValidatedSchemaCache cache, bool serialize, int iterations)
        {
            var timer = Stopwatch.StartNew();
            for (int i = 0; i < iterations; i++)
            {
                var response = SnapshotReader.Capture(source, cache);
                if (serialize) Program.SerializeResponse(response);
            }
            timer.Stop();
            return timer.Elapsed.TotalMilliseconds / iterations;
        }

        private static void BenchmarkCache()
        {
            const int iterations = 80;
            var source = BenchmarkSource();
            var cache = new ValidatedSchemaCache();
            for (int i = 0; i < 12; i++)
            {
                Program.SerializeResponse(SnapshotReader.Capture(source));
                Program.SerializeResponse(SnapshotReader.Capture(source, cache));
            }
            var result = new Dictionary<string, object>
            {
                { "evidence", "SYNTHETIC_OFFLINE_ONLY" }, { "variables", 335 },
                { "session_bytes", 16384 }, { "iterations_per_batch", iterations }
            };
            foreach (bool serialize in new bool[] { false, true })
            {
                var cold = new double[3];
                var warm = new double[3];
                for (int repeat = 0; repeat < 3; repeat++)
                {
                    if (repeat % 2 == 0)
                    {
                        cold[repeat] = Measure(source, null, serialize, iterations);
                        warm[repeat] = Measure(source, cache, serialize, iterations);
                    }
                    else
                    {
                        warm[repeat] = Measure(source, cache, serialize, iterations);
                        cold[repeat] = Measure(source, null, serialize, iterations);
                    }
                }
                Array.Sort(cold); Array.Sort(warm);
                string suffix = serialize ? "capture_and_json_ms" : "capture_ms";
                result["uncached_" + suffix] = cold[1];
                result["cached_" + suffix] = warm[1];
            }
            Console.Out.WriteLine(Program.SerializeResponse(result));
        }
    }
}
