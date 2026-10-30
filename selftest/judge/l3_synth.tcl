# L3 判定：对 TopModule 做 OOC 综合，产出 JSON 结果。
# 由 tools/veval_survey.tcl 改造而来（该脚本原本对 RefModule 做资源调查）。
#
# 用法： vivado -mode batch -source l3_synth.tcl -tclargs <part> <dut.sv> <top> <clk_ns> <out_json>
#
# 判定口径（评测执行手册 步骤 4）：synth_design 产出网表且无 ERROR 即 L3 通过。
# 综合超时由外层 veval-judge 的墙钟计时处理，超时按 L2 计分并标注。

set part     [lindex $argv 0]
set dut      [lindex $argv 1]
set top      [lindex $argv 2]
set clk_ns   [lindex $argv 3]
set out_json [lindex $argv 4]

proc emit {out_json d} {
    set fh [open $out_json w]
    puts $fh "{"
    set n [llength $d]
    for {set i 0} {$i < $n} {incr i 2} {
        set k [lindex $d $i]
        set v [lindex $d [expr {$i + 1}]]
        set sep [expr {$i + 2 < $n ? "," : ""}]
        # 数值裸写，其余加引号
        if {[string is double -strict $v] || [string is integer -strict $v]} {
            puts $fh "  \"$k\": $v$sep"
        } else {
            puts $fh "  \"$k\": \"$v\"$sep"
        }
    }
    puts $fh "}"
    close $fh
}

create_project -in_memory -part $part

if {[catch {read_verilog -sv $dut} err]} {
    puts "L3_READ_FAIL: $err"
    emit $out_json [list status READ_FAIL message [string map {\" ' \n " "} $err]]
    exit 0
}

if {[catch {synth_design -top $top -mode out_of_context -part $part} err]} {
    puts "L3_SYNTH_FAIL: $err"
    emit $out_json [list status SYNTH_FAIL message [string map {\" ' \n " "} $err]]
    exit 0
}

# 资源统计。沿用 veval_survey.tcl 的 get_cells 过滤写法，失败分析要用。
set lut   [llength [get_cells -quiet -hier -filter {REF_NAME =~ LUT*}]]
set ff    [llength [get_cells -quiet -hier -filter {REF_NAME =~ FD*}]]
set dsp   [llength [get_cells -quiet -hier -filter {REF_NAME =~ DSP*}]]
set bram  [llength [get_cells -quiet -hier -filter {REF_NAME =~ RAMB*}]]
set srl   [llength [get_cells -quiet -hier -filter {REF_NAME =~ SRL*}]]
set carry [llength [get_cells -quiet -hier -filter {REF_NAME =~ CARRY*}]]
set muxf  [llength [get_cells -quiet -hier -filter {REF_NAME =~ MUXF*}]]

# 寄存器间 Fmax。156 题中仅 55 题存在寄存器到寄存器的路径（待办 12 的实测结论），
# 其余为纯组合逻辑，此处 has_clk / fmax 会留空。不参与 L3 判定，仅作记录。
set has_clk 0
set wns ""
set fmax ""
if {[llength [get_ports -quiet clk]] > 0} {
    set has_clk 1
    create_clock -name clk -period $clk_ns [get_ports clk]
    set regs [all_registers]
    if {[llength $regs] > 0} {
        set p [get_timing_paths -quiet -from $regs -to $regs -delay_type max -max_paths 1 -nworst 1]
        if {[llength $p] > 0} {
            set wns [get_property -quiet SLACK $p]
            if {$wns ne "" && [string is double -strict $wns]} {
                set delay [expr {$clk_ns - $wns}]
                if {$delay > 0} { set fmax [format "%.1f" [expr {1000.0 / $delay}]] }
            } else { set wns "" }
        }
    }
}

emit $out_json [list status OK LUT $lut FF $ff DSP $dsp BRAM $bram \
                     SRL $srl CARRY $carry MUXF $muxf \
                     has_clk $has_clk WNS_ns $wns Fmax_MHz $fmax]

puts "L3_OK: LUT=$lut FF=$ff DSP=$dsp BRAM=$bram Fmax=$fmax"
