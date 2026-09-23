"""Dump the pinned botocore shapes for bedrock-agentcore CreateEvent/ListEvents.

Standalone (no AWS calls): confirms required members and payload/blob types so
the Memory adapter is written against the real operation model.
"""

from __future__ import annotations

import botocore.session


def main() -> None:
    session = botocore.session.get_session()
    model = session.get_service_model("bedrock-agentcore")
    print("botocore", botocore.__version__)
    for op_name in ("CreateEvent", "ListEvents"):
        op = model.operation_model(op_name)
        inp = op.input_shape
        print(f"\n== {op_name} input: required={sorted(inp.required_members)}")
        for name, shape in inp.members.items():
            print(f"  {name}: {shape.type_name} ({shape.name})")
        out = op.output_shape
        print(f"-- {op_name} output members: {list(out.members)}")

    payload = model.shape_for("PayloadTypeList").member
    print(f"\nPayloadType ({payload.type_name}) members:")
    for name, shape in payload.members.items():
        print(f"  {name}: {shape.type_name} ({shape.name})")
        if shape.type_name == "structure":
            for sub, subshape in shape.members.items():
                print(f"     .{sub}: {subshape.type_name} ({subshape.name})")
    event = model.shape_for("Event")
    print(f"\nEvent members: {list(event.members)}")


if __name__ == "__main__":
    main()
