"""Test the new setters and real command dispatch with g++ on the host.

Run: python3 Tools/test_commands.py
"""

import re
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SETTERS = {
    "SetHeatToCoolTemp": (
        95,
        [("21", 149), ("-127", 1), ("127", 255), ("0", 128)],
        ["-128", "128"],
    ),
    "SetCoolToHeatTemp": (
        96,
        [("12", 140), ("-127", 1), ("127", 255)],
        ["-128", "128"],
    ),
    "SetRoomOperationMaxTime": (
        97,
        [("90", 4), ("30", 2), ("7620", 255)],
        ["0", "29", "31", "7650"],
    ),
    "SetDHWHeatupMaxTime": (
        98,
        [("105", 106), ("1", 2), ("254", 255)],
        ["0", "255", "-1"],
    ),
    "SetQuietModeSchedule": (7, [("0", 64), ("1", 128)], ["2", "-1"]),
}

SHIM = r"""
#include <cassert>
#include <cstring>
#include <cstdio>
#include <vector>
using byte = unsigned char;
#define PROGMEM
#define memcpy_P memcpy
#define snprintf_P snprintf
#define PSTR(x) x
std::vector<byte> sent;
int send_count;
bool send_command(byte* data, int length) {
  ++send_count; sent.assign(data, data + length); return true;
}
void log_message(char*) {}
"""

# Commands that are not sent while the heat pump reports the requested value:
# readback topic and payloads that must read back unchanged. The table in
# commands.cpp must list exactly these commands.
UNCHANGED = {
    "SetHeatpump": (0, "01"),
    "SetMaxPumpDuty": (95, ["0", "64", "254"]),
    "SetQuietMode": (18, "0123"),
    "SetZ1HeatRequestTemperature": (27, ["-5", "0", "35"]),
    "SetZ1CoolRequestTemperature": (28, ["-5", "18"]),
    "SetZ2HeatRequestTemperature": (34, ["5", "40"]),
    "SetZ2CoolRequestTemperature": (35, ["-1", "20"]),
    "SetSterilizationTemp": (70, ["55", "75"]),
    "SetSterilizationMaxTime": (71, ["5", "60"]),
    "SetOperationMode": (4, "01345"),
    "SetDHWTemp": (9, ["40", "48", "75"]),
    "SetZones": (94, "012"),
    "SetFloorHeatDelta": (23, ["1", "15"]),
    "SetFloorCoolDelta": (24, ["1", "15"]),
    "SetDHWHeatDelta": (22, ["-12", "-2"]),
    "SetHeaterDelayTime": (96, ["0", "30"]),
    "SetHeaterStartDelta": (97, ["-5", "1"]),
    "SetHeaterStopDelta": (98, ["2", "10"]),
    "SetMainSchedule": (13, "01"),
    "SetAltExternalSensor": (108, "01"),
    "SetExternalPadHeater": (114, "012"),
    "SetBufferDelta": (113, ["0", "10"]),
    "SetBuffer": (99, "01"),
    "SetHeatingOffOutdoorTemp": (77, ["5", "35"]),
    "SetExternalControl": (119, "01"),
    "SetExternalError": (121, "01"),
    "SetExternalCompressorControl": (122, "01"),
    "SetExternalHeatCoolControl": (120, "01"),
    "SetBivalentControl": (129, "01"),
    "SetBivalentMode": (130, "012"),
    "SetBivalentStartTemp": (131, ["-15", "35"]),
    "SetBivalentAPStartTemp": (134, ["-15", "0"]),
    "SetBivalentAPStopTemp": (135, ["10", "35"]),
    "SetHeatingControl": (139, "01"),
    "SetSmartDHW": (140, "01"),
    "SetQuietModePriority": (141, "01"),
    "SetPumpFlowrateMode": (106, "01"),
    "SetDHWSensorSelection": (143, "01"),
    "SetDHWHeaterState": (58, "01"),
    "SetRoomHeaterState": (59, "01"),
    "SetHeaterOnOutdoorTemp": (78, ["-15", "20"]),
    "SetHeatToCoolTemp": (79, ["-127", "21", "127"]),
    "SetCoolToHeatTemp": (80, ["12"]),
    "SetRoomOperationMaxTime": (158, ["30", "90", "7620"]),
    "SetDHWHeatupMaxTime": (159, ["1", "254"]),
    "SetQuietModeSchedule": (3, "01"),
}
# The command writes another code than the heat pump reports (Auto, Auto+DHW).
NEVER_UNCHANGED = {"SetOperationMode": (4, "26"), "SetForceDHW": (2, "01")}

STRING_SHIM = r"""
#include <cstdlib>
#include <string>
class String {
  std::string value;
public:
  String(const char* s): value(s ? s : "") {}
  String(int n): value(std::to_string(n)) {}
  String(float n, int digits) {
    char buf[64]; snprintf(buf, sizeof(buf), "%.*f", digits, n); value = buf;
  }
  const char* c_str() const { return value.c_str(); }
  int toInt() const { return atoi(c_str()); }
};
const char* reported;
bool skip(const char* name, unsigned char* cmd, unsigned int len) {
  return heatpump_command_unchanged(name, cmd, len, reported);
}
"""


def check_unchanged(header, source, decode_header, decode_source):
    """Every listed command is skipped exactly when the heat pump reports its value."""
    tables = {}
    for name in ("topicBytes", "topicFunctions"):
        body = re.search(r"\b" + name + r"\[\].*?=\s*\{(.*?)\};", decode_header, re.S)[1]
        tables[name] = [
            line.split("//")[0].strip().rstrip(",")
            for line in body.splitlines()
            if line.split("//")[0].strip()
        ]
    masks = dict(re.findall(r'\{ "(\w+)", (0x[0-9a-f]+) \}', source))
    assert set(masks) == set(UNCHANGED), set(masks) ^ set(UNCHANGED)
    setters = source[
        source.index("unsigned int set_heatpump_state(") : source.index(
            "//start of optional pcb commands"
        )
    ]
    setters = re.sub(
        r"unsigned int set_curves\(.*?(?=unsigned int set_zones)",
        "unsigned int set_curves(char*, unsigned char*, char*) { return 0; }\n",
        setters,
        flags=re.S,
    )
    stubs = [
        f"unsigned int {name}({args}) {{ return 0; }}"
        for name, args in re.findall(r"unsigned int (set_\w+)\(([^;]+)\);", header)
        if f"unsigned int {name}(" not in setters
    ]
    decoders = decode_source[
        decode_source.index("String getRawByte(") : decode_source.index(
            "String getModel("
        )
    ]
    template = re.search(r"byte panasonicSendQuery\[\].*?\};", source, re.S)[0]
    dispatcher = source[
        source.index("void send_heatpump_command(") : source.index(
            "bool saveOptionalPCB("
        )
    ]
    header = re.sub(r"^#include.*$", "", header, flags=re.M)
    checks = []
    cases = 0
    entries = [(name, *entry, "true") for name, entry in UNCHANGED.items()]
    entries += [(name, *entry, "false") for name, entry in NEVER_UNCHANGED.items()]
    for name, topic, payloads, same in entries:
        address = int(tables["topicBytes"][topic])
        decoder = tables["topicFunctions"][topic]
        mask = masks.get(name, "0")
        for payload in payloads:
            checks.append(
                f"""
  {{ char topic[] = "{name}", payload[] = "{payload}";
    sent.clear(); send_count = 0;
    send_heatpump_command(topic, payload, send_command, log_message, false);
    std::vector<byte> cmd = sent;
    assert(send_count == 1 && cmd[{address}] != 0);
    for (unsigned int i = 4; i < cmd.size(); ++i) assert(i == {address} || cmd[i] == 0);
    char data[DATASIZE]; memset(data, 0x55, sizeof(data)); data[0] = 0x71;
    reported = data;
    for (int b = 0; b < 256; ++b) {{
      data[{address}] = b;
      bool reports = std::string({decoder}(b).c_str()) == "{payload}";
      bool unchanged = heatpump_command_unchanged(topic, cmd.data(), cmd.size(), data);
      assert({same} ? unchanged == reports : !(unchanged && reports));
      send_count = 0;
      send_heatpump_command(topic, payload, send_command, log_message, false, skip);
      assert(send_count == (unchanged ? 0 : 1));
      if (unchanged) {{
        assert((cmd[{address}] & ~{mask}) == 0);
        data[0] = 0;  // no data block received yet
        assert(!heatpump_command_unchanged(topic, cmd.data(), cmd.size(), data));
        data[0] = 0x71;
      }}
    }}
  }}
"""
            )
            cases += 1
    program = SHIM + header + STRING_SHIM + template + setters + "\n".join(stubs)
    program += decoders + dispatcher
    program += "\nint main() {\n" + "".join(checks) + "\n}\n"
    with tempfile.TemporaryDirectory() as directory:
        cpp = Path(directory) / "test.cpp"
        binary = Path(directory) / "test"
        cpp.write_text(program)
        subprocess.run(
            ["g++", "-std=c++17", "-funsigned-char", str(cpp), "-o", str(binary)],
            check=True,
        )
        subprocess.run([str(binary)], check=True)
    print(f"PASS: {cases} unchanged-value cases (sent unless the heat pump reports it)")


def main():
    header = (ROOT / "HeishaMon/commands.h").read_text()
    source = (ROOT / "HeishaMon/commands.cpp").read_text()
    docs = (ROOT / "MQTT-Topics.md").read_text()
    additions = source[
        source.index("static unsigned int set_integer_field") : source.index(
            "unsigned int set_sterilization_temp"
        )
    ]
    real_functions = set(re.findall(r"unsigned int (\w+)\(", additions))
    stubs = []
    for name, args in re.findall(r"unsigned int (set_\w+)\(([^;]+)\);", header):
        if name not in real_functions:
            stubs.append(f"unsigned int {name}({args}) {{ return 0; }}")
    template = re.search(r"byte panasonicSendQuery\[\].*?\};", source, re.S)[0]
    dispatcher = source[
        source.index("void send_heatpump_command(") : source.index(
            "bool saveOptionalPCB("
        )
    ]
    header = re.sub(r"^#include.*$", "", header, flags=re.M)
    checks = []
    cases = 0
    for topic, (address, accepted, rejected) in SETTERS.items():
        assert f"| {topic} |" in docs
        function = re.search(r'\{\s*"' + topic + r'",\s*(\w+)\s*\}', header)[1]
        for payload, encoded in accepted:
            checks.append(
                f"""
  {{ char topic[] = "{topic}", payload[] = "{payload}";
    sent.clear(); send_count = 0;
    send_heatpump_command(topic, payload, send_command, log_message, false);
    assert(send_count == 1 && sent.size() == sizeof(panasonicSendQuery));
    for (unsigned int i = 0; i < sent.size(); ++i)
      assert(sent[i] == (i == {address} ? {encoded} : panasonicSendQuery[i]));
  }}
"""
            )
            cases += 1
        for payload in rejected + [
            "",
            "abc",
            "1x",
            "1.0",
            " 1",
            "+1",
            "1 ",
            "-",
            "999999999999999",
        ]:
            checks.append(
                f"""
  {{ char topic[] = "{topic}", payload[] = "{payload}";
    sent.clear(); send_count = 0;
    send_heatpump_command(topic, payload, send_command, log_message, false);
    assert(send_count == 0 && sent.empty());
    byte command[256]; memset(command, 0xa5, sizeof(command));
    char log[256];
    assert({function}(payload, command, log) == 0);
    for (byte value : command) assert(value == 0xa5);
  }}
"""
            )
            cases += 1
        checks.append(
            f"""
  {{ byte command[256]; memset(command, 0xa5, sizeof(command)); char log[256];
    assert({function}(nullptr, command, log) == 0);
    for (byte value : command) assert(value == 0xa5);
  }}
"""
        )
        cases += 1
    program = SHIM + header + template + additions + "\n".join(stubs) + dispatcher
    program += "\nint main() {\n" + "".join(checks) + "\n}\n"
    with tempfile.TemporaryDirectory() as directory:
        cpp = Path(directory) / "test.cpp"
        binary = Path(directory) / "test"
        cpp.write_text(program)
        subprocess.run(["g++", "-std=c++17", str(cpp), "-o", str(binary)], check=True)
        subprocess.run([str(binary)], check=True)
    print(f"PASS: {cases} setter/dispatch cases (bytes, boundaries, rejected payloads)")
    check_unchanged(
        (ROOT / "HeishaMon/commands.h").read_text(),
        source,
        (ROOT / "HeishaMon/decode.h").read_text(),
        (ROOT / "HeishaMon/decode.cpp").read_text(),
    )


if __name__ == "__main__":
    main()
