// Reference solution for ex01_popcount8.
//
// Only purpose: proving the judging pipeline reaches L3 on a known-good input.
// Do not feed it to your agent -- a self-test that passes because the answer was
// in the prompt measures nothing.

module TopModule (
  input  [7:0] in,
  output [3:0] out
);

  integer i;
  reg [3:0] count;

  always @(*) begin
    count = 4'd0;
    for (i = 0; i < 8; i = i + 1)
      count = count + in[i];
  end

  assign out = count;

endmodule
