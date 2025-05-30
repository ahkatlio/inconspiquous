from xdsl.dialects import arith, builtin
from xdsl.dialects.builtin import IntegerAttr, i1
from xdsl.parser import Context
from xdsl.passes import ModulePass
from xdsl.pattern_rewriter import (
    GreedyRewritePatternApplier,
    PatternRewriter,
    RewritePattern,
    op_type_rewrite_pattern,
)
from xdsl.ir import Operation, SSAValue
from typing import List, Optional, Tuple, cast
from inconspiquous.dialects import qssa
from inconspiquous.dialects.angle import AngleAttr, CondNegateAngleOp, ConstantAngleOp
from inconspiquous.dialects.gate import (
    XZOp,
    CliffordGateAttr,
)
from inconspiquous.dialects.measurement import (
    XYDynMeasurementOp,
    XYMeasurementAttr,
)
from inconspiquous.transforms.xzs.fusion import FuseXZGatesPattern
from inconspiquous.utils.linear_walker import LinearWalker


def _create_constant_bool(rewriter: PatternRewriter, value: bool) -> Operation:
    """Helper to create an i1 constant (boolean) value."""
    return rewriter.create_op(
        arith.ConstantOp,
        properties={"value": IntegerAttr(1 if value else 0, i1)},
        result_types=[i1],
    )


class XZCommutePattern(RewritePattern):
    """Commute an XZ gadget past a Hadamard/CX/CZ gate, or MeasureOp."""

    @op_type_rewrite_pattern
    def match_and_rewrite(self, op1: qssa.DynGateOp, rewriter: PatternRewriter):
        gate = op1.gate.owner
        if not isinstance(gate, XZOp):
            return
        if len(op1.outs[0].uses) != 1:
            return

        (use,) = op1.outs[0].uses
        op2 = use.operation

        # Handle XY measurement case (unchanged)
        angle = None
        if isinstance(op2, qssa.DynMeasureOp) and isinstance(
            op2.measurement.owner, XYDynMeasurementOp
        ):
            angle = op2.measurement.owner.angle

        if isinstance(op2, qssa.MeasureOp) and isinstance(
            op2.measurement, XYMeasurementAttr
        ):
            angle = op2.measurement.angle

        if angle is not None:
            if isinstance(angle, AngleAttr):
                angle_op = (ConstantAngleOp(angle),)
                angle = angle_op[0].out
            else:
                angle_op = ()

            negate = CondNegateAngleOp(gate.x, angle)
            new_measurement = XYDynMeasurementOp(negate)
            new_op2 = qssa.DynMeasureOp(op1.ins[0], measurement=new_measurement)
            new_op1 = arith.XOrIOp(new_op2.outs[0], gate.z)

            rewriter.replace_op(
                op2, (*angle_op, negate, new_measurement, new_op2, new_op1)
            )
            rewriter.erase_op(op1)
            return

        # Handle computational basis measurement case (unchanged)
        if isinstance(op2.gate, CliffordGateAttr):
            clifford = op2.gate
            input_index = use.index
            x_in: SSAValue = gate.x
            z_in: SSAValue = gate.z
            new_operands: List[SSAValue] = list(op2.ins)
            new_operands[input_index] = op1.ins[0]
            new_gate_op: Operation = rewriter.create_op(  # type: ignore
                qssa.GateOp,
                properties={"gate": clifford},
                operands=new_operands,
                result_types=[out.type for out in op2.outs],
            )
            # Get propagation rules for X and Z inputs
            x_prop_rules: Tuple[Tuple[bool, bool], ...] = clifford.pauli_prop(
                input_index, "X"
            )
            z_prop_rules: Tuple[Tuple[bool, bool], ...] = clifford.pauli_prop(
                input_index, "Z"
            )
            # Keep track of all new operations and output results
            new_ops: List[Operation] = [new_gate_op]
            results: List[SSAValue] = []

            # Process each output qubit
            for i, output_qubit in enumerate(cast(List[SSAValue], new_gate_op.outs)):
                # How X input affects this output qubit: (X component, Z component)
                x_from_x, z_from_x = x_prop_rules[i]

                # How Z input affects this output qubit: (X component, Z component)
                x_from_z, z_from_z = z_prop_rules[i]

                new_x: Optional[SSAValue] = None

                # X component from input X
                if x_from_x:
                    new_x = x_in

                # X component from input Z
                if x_from_z:
                    if new_x is not None:
                        # Need to XOR with the existing value
                        new_x_op: Operation = rewriter.create_op(  # type: ignore
                            arith.XOrIOp, operands=[new_x, z_in], result_types=[i1]
                        )
                        new_ops.append(new_x_op)
                        new_x = new_x_op.results[0]
                    else:
                        new_x = z_in

                # If no X component, use constant 0
                if new_x is None:
                    const_op = _create_constant_bool(rewriter, False)
                    new_ops.append(const_op)
                    new_x = const_op.results[0]

                # Create the necessary operations to compute new Z value
                new_z: Optional[SSAValue] = None

                # Z component from input X
                if z_from_x:
                    new_z = x_in

                # Z component from input Z
                if z_from_z:
                    if new_z is not None:
                        # Need to XOR with the existing value
                        new_z_op: Operation = rewriter.create_op(  # type: ignore
                            arith.XOrIOp, operands=[new_z, z_in], result_types=[i1]
                        )
                        new_ops.append(new_z_op)
                        new_z = new_z_op.results[0]
                    else:
                        new_z = z_in

                # If no Z component, use constant 0
                if new_z is None:
                    const_op = _create_constant_bool(rewriter, False)
                    new_ops.append(const_op)
                    new_z = const_op.results[0]

                new_xz_op: Operation = rewriter.create_op(  # type: ignore
                    XZOp,
                    operands=[new_x, new_z],
                    result_types=[gate.out.type],
                )
                new_ops.append(new_xz_op)

                # Apply the new XZ operation to the output qubit
                new_dyn_gate: Operation = rewriter.create_op(  # type: ignore
                    qssa.DynGateOp,
                    operands=[new_xz_op.results[0], output_qubit],
                    result_types=[output_qubit.type],
                )
                new_ops.append(new_dyn_gate)
                results.append(new_dyn_gate.results[0])

            # Replace the original gate operation with our new sequence
            rewriter.replace_op(op2, new_ops, results)  # type: ignore
            rewriter.erase_op(op1)
            return


class XZCommute(ModulePass):
    name = "xz-commute"

    def apply(self, ctx: Context, op: builtin.ModuleOp) -> None:
        LinearWalker(
            GreedyRewritePatternApplier([FuseXZGatesPattern(), XZCommutePattern()])
        ).rewrite_module(op)
