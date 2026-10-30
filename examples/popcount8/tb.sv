// Official testbench for ex01_popcount8.
//
// NOT handed to the agent -- this is what decides L2. Instantiates RefModule and
// TopModule side by side and compares every sample. The final line
//
//     Mismatches: <n> in <m> samples
//
// is what the judge parses, and matches the format VerilogEval v2 emits so the
// two are interchangeable.

`timescale 1ps/1ps

module tb();

  int errors = 0;
  int clocks = 0;

  reg clk = 0;
  initial forever #5 clk = ~clk;

  logic [7:0] in;
  logic [3:0] out_ref;
  logic [3:0] out_dut;

  RefModule good1 (.in(in), .out(out_ref));
  TopModule  dut1  (.in(in), .out(out_dut));

  // XOR form: an X in the reference matches anything, an X in the DUT does not.
  wire tb_match = (out_ref === (out_ref ^ out_dut ^ out_ref));

  initial begin
    // Exhaustive: 256 inputs is small enough that sampling would be a choice
    // with no upside.
    for (int i = 0; i < 256; i++) begin
      @(negedge clk) in <= i[7:0];
    end
    repeat (2) @(negedge clk);
    $finish;
  end

  always @(posedge clk) begin
    if ($time > 10) begin
      clocks++;
      if (!tb_match) errors++;
    end
  end

  final begin
    $display("Hint: Total mismatched samples is %1d out of %1d samples", errors, clocks);
    $display("Mismatches: %1d in %1d samples", errors, clocks);
  end

  initial begin
    #1000000;
    $display("TIMEOUT");
    $finish;
  end

endmodule
