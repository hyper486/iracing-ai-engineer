// Adapted from Crew Chief iRSDKSharp/CVarHeader.cs.
// Copyright (c) 2019-2024 Britton IT Ltd. MIT; see LICENSE.CrewChief.
// Source revision and modifications are recorded in UPSTREAM.md.
using System.Collections.Generic;

namespace Aeis.CrewChiefReader
{
    internal sealed class CVarHeader
    {
        internal enum VarType { irChar, irBool, irInt, irBitField, irFloat, irDouble }

        internal CVarHeader(int type, int offset, int count, string name,
            string desc, string unit, bool countAsTime)
        {
            Type = (VarType)type;
            Offset = offset;
            Count = count;
            Name = name;
            Desc = desc;
            Unit = unit;
            CountAsTime = countAsTime;
        }

        internal VarType Type { get; private set; }
        internal int Offset { get; private set; }
        internal int Count { get; private set; }
        internal string Name { get; private set; }
        internal string Desc { get; private set; }
        internal string Unit { get; private set; }
        internal bool CountAsTime { get; private set; }

        internal int Bytes
        {
            get
            {
                if (Type == VarType.irChar || Type == VarType.irBool) return 1;
                if (Type == VarType.irInt || Type == VarType.irBitField ||
                    Type == VarType.irFloat) return 4;
                if (Type == VarType.irDouble) return 8;
                return 0;
            }
        }

        internal Dictionary<string, object> ToProtocol()
        {
            string[] names = { "char", "bool", "int32", "uint32_or_bitfield", "float32", "float64" };
            return new Dictionary<string, object>
            {
                { "name", Name }, { "type_code", (int)Type },
                { "dtype", names[(int)Type] }, { "offset", Offset },
                { "count", Count }, { "count_as_time", CountAsTime },
                { "unit", Unit }, { "description", Desc }
            };
        }
    }
}
