// Private local transport only. Do not persist raw responses as public evidence.
using System;
using System.Collections.Generic;
using System.IO;
using System.Text;
using System.Web.Script.Serialization;

namespace Aeis.CrewChiefReader
{
    internal static class Program
    {
        internal const int MaximumOutputBytes = 16777216;
        private static readonly object SerializerLock = new object();
        private static readonly JavaScriptSerializer Serializer = new JavaScriptSerializer
        {
            MaxJsonLength = MaximumOutputBytes - 2, RecursionLimit = 32
        };

        private static int Main(string[] args)
        {
            Console.OutputEncoding = new UTF8Encoding(false);
            if (args.Length != 0)
            {
                Console.Out.WriteLine(SerializeResponse(SnapshotReader.Failure("unavailable", "invalid_arguments")));
                return 2;
            }
            bool tooLong;
            string request;
            var schemaCache = new ValidatedSchemaCache();
            while ((request = ReadRequest(Console.In, out tooLong)) != null)
            {
                Dictionary<string, object> response;
                if (tooLong || request != "snapshot")
                    response = SnapshotReader.Failure("unavailable", "invalid_request");
                else
                {
                    try
                    {
                        using (var source = new LiveSharedMemoryReader()) response = SnapshotReader.Capture(source, schemaCache);
                    }
                    catch (FileNotFoundException)
                    {
                        schemaCache.Clear();
                        response = SnapshotReader.Failure("unavailable", "mapping_unavailable");
                    }
                    catch (UnauthorizedAccessException)
                    {
                        schemaCache.Clear();
                        response = SnapshotReader.Failure("unavailable", "mapping_access_denied");
                    }
                    catch (Exception)
                    {
                        schemaCache.Clear();
                        response = SnapshotReader.Failure("unavailable", "snapshot_failed");
                    }
                }
                string json = SerializeResponse(response);
                try
                {
                    Console.Out.WriteLine(json);
                    Console.Out.Flush();
                }
                catch (IOException) { return 1; }
            }
            return 0;
        }

        internal static string SerializeResponse(Dictionary<string, object> response)
        {
            // The production loop is serial; also serialize concurrent internal
            // callers so no assumption about serializer thread safety is needed.
            lock (SerializerLock) return SerializeLocked(response);
        }

        private static string SerializeLocked(Dictionary<string, object> response)
        {
            try
            {
                string json = Serializer.Serialize(response);
                // Console.Out.WriteLine uses CRLF on Windows; include both bytes.
                if (Encoding.UTF8.GetByteCount(json) + 2 > MaximumOutputBytes)
                    return Serializer.Serialize(SnapshotReader.Failure("inconsistent", "output_limit"));
                return json;
            }
            catch (InvalidOperationException)
            {
                return Serializer.Serialize(SnapshotReader.Failure("inconsistent", "output_limit"));
            }
            catch (Exception)
            {
                return Serializer.Serialize(SnapshotReader.Failure("unavailable", "serialization_failed"));
            }
        }

        // Bound memory even for malformed requests; consume through the newline
        // so each request still receives exactly one fixed-code response.
        internal static string ReadRequest(TextReader input, out bool tooLong)
        {
            var text = new StringBuilder();
            tooLong = false;
            bool any = false;
            int next;
            while ((next = input.Read()) >= 0)
            {
                any = true;
                if (next == '\n') break;
                if (text.Length < 64) text.Append((char)next);
                else tooLong = true;
            }
            if (!any) return null;
            if (text.Length > 0 && text[text.Length - 1] == '\r') text.Length--;
            return text.ToString();
        }
    }
}
