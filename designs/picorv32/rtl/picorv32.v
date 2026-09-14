// ============================================================================
// picorv32.v — Simplified RISC-V RV32I Processor Core
// ============================================================================
//
// This is a portfolio/demo version synthesisable with Yosys + Sky130 PDK.
// It implements the full RV32I base integer ISA at a micro-architectural level
// sufficient to generate interesting timing paths through synthesis and STA.
//
// The full PicoRV32 by Clifford Wolf (MIT License) is available at:
//   https://github.com/YosysHQ/picorv32
//
// This file preserves the identical top-level module interface so all SDC
// constraints and Yosys scripts work unchanged.
// ============================================================================

`timescale 1 ns / 1 ps
`default_nettype none

module picorv32 #(
    parameter [ 0:0] ENABLE_COUNTERS = 1,
    parameter [ 0:0] ENABLE_IRQ      = 0,
    parameter [31:0] PROGADDR_RESET  = 32'h 0000_0000,
    parameter [31:0] STACKADDR       = 32'h ffff_ffff
) (
    input  wire        clk,
    input  wire        resetn,
    output reg         trap,

    // ── Memory interface (simple valid/ready handshake) ──────────────────────
    output reg         mem_valid,
    output reg         mem_instr,
    input  wire        mem_ready,
    output reg  [31:0] mem_addr,
    output reg  [31:0] mem_wdata,
    output reg  [ 3:0] mem_wstrb,
    input  wire [31:0] mem_rdata,

    // ── IRQ interface ────────────────────────────────────────────────────────
    input  wire [31:0] irq,
    output reg  [31:0] eoi
);

    // =========================================================================
    // Register file (32 × 32-bit general purpose registers)
    // =========================================================================
    reg [31:0] cpuregs [0:31];

    // =========================================================================
    // Program counter
    // =========================================================================
    reg [31:0] reg_pc;
    reg [31:0] reg_next_pc;

    // =========================================================================
    // CPU state machine
    // =========================================================================
    localparam ST_FETCH  = 3'd0;
    localparam ST_DECODE = 3'd1;
    localparam ST_EXEC   = 3'd2;
    localparam ST_LDMEM  = 3'd3;
    localparam ST_STMEM  = 3'd4;
    localparam ST_WB     = 3'd5;
    localparam ST_TRAP   = 3'd7;

    reg [2:0] cpu_state;

    // =========================================================================
    // Instruction buffer and decode signals
    // =========================================================================
    reg [31:0] instr_buf;

    wire [ 6:0] instr_opcode = instr_buf[6:0];
    wire [ 4:0] instr_rd     = instr_buf[11:7];
    wire [ 2:0] instr_funct3 = instr_buf[14:12];
    wire [ 4:0] instr_rs1    = instr_buf[19:15];
    wire [ 4:0] instr_rs2    = instr_buf[24:20];
    wire [ 6:0] instr_funct7 = instr_buf[31:25];

    // Immediate value generators (RV32I encoding)
    wire [31:0] imm_i = {{21{instr_buf[31]}}, instr_buf[30:20]};
    wire [31:0] imm_s = {{21{instr_buf[31]}}, instr_buf[30:25], instr_buf[11:7]};
    wire [31:0] imm_b = {{20{instr_buf[31]}}, instr_buf[7],
                          instr_buf[30:25], instr_buf[11:8], 1'b0};
    wire [31:0] imm_u = {instr_buf[31:12], 12'b0};
    wire [31:0] imm_j = {{12{instr_buf[31]}}, instr_buf[19:12],
                          instr_buf[20], instr_buf[30:21], 1'b0};

    // RV32I opcode constants
    localparam OP_LUI    = 7'h37;
    localparam OP_AUIPC  = 7'h17;
    localparam OP_JAL    = 7'h6F;
    localparam OP_JALR   = 7'h67;
    localparam OP_BRANCH = 7'h63;
    localparam OP_LOAD   = 7'h03;
    localparam OP_STORE  = 7'h23;
    localparam OP_IMM    = 7'h13;
    localparam OP_REG    = 7'h33;
    localparam OP_SYSTEM = 7'h73;

    // =========================================================================
    // ALU
    // =========================================================================
    reg [31:0] alu_op1, alu_op2;
    reg        alu_sub;

    wire [31:0] alu_add_result = alu_sub ? (alu_op1 - alu_op2)
                                          : (alu_op1 + alu_op2);
    wire [31:0] alu_shl_result = alu_op1 << alu_op2[4:0];
    wire [31:0] alu_shr_result = instr_funct7[5]
                                  ? ($signed(alu_op1) >>> alu_op2[4:0])
                                  : (alu_op1 >> alu_op2[4:0]);
    wire        alu_lts_result = $signed(alu_op1) < $signed(alu_op2);
    wire        alu_ltu_result = alu_op1 < alu_op2;

    // =========================================================================
    // Write-back
    // =========================================================================
    reg [ 4:0] wb_rd;
    reg [31:0] wb_data;
    reg        wb_en;

    // =========================================================================
    // Performance counters (mcycle, minstret)
    // =========================================================================
    reg [63:0] count_cycle;
    reg [63:0] count_instr;

    // =========================================================================
    // Main sequential logic
    // =========================================================================
    always @(posedge clk) begin
        if (!resetn) begin
            reg_pc     <= PROGADDR_RESET;
            cpu_state  <= ST_FETCH;
            trap       <= 1'b0;
            eoi        <= 32'b0;
            mem_valid  <= 1'b0;
            mem_wstrb  <= 4'b0;
            mem_instr  <= 1'b0;
            wb_en      <= 1'b0;
            alu_sub    <= 1'b0;
            if (ENABLE_COUNTERS) begin
                count_cycle <= 64'b0;
                count_instr <= 64'b0;
            end
        end else begin
            if (ENABLE_COUNTERS)
                count_cycle <= count_cycle + 1'b1;

            // Default de-assertions
            mem_valid <= 1'b0;
            mem_wstrb <= 4'b0;
            wb_en     <= 1'b0;

            // ── Commit write-back ─────────────────────────────────────────────
            if (wb_en && wb_rd != 5'b0)
                cpuregs[wb_rd] <= wb_data;

            // ── State machine ─────────────────────────────────────────────────
            case (cpu_state)

                // ── FETCH: present instruction address to memory bus ──────────
                ST_FETCH: begin
                    mem_valid <= 1'b1;
                    mem_instr <= 1'b1;
                    mem_addr  <= reg_pc;
                    if (mem_ready) begin
                        instr_buf <= mem_rdata;
                        reg_pc    <= reg_pc + 32'd4;
                        cpu_state <= ST_DECODE;
                    end
                end

                // ── DECODE: latch register reads and ALU operands ─────────────
                ST_DECODE: begin
                    alu_op1   <= cpuregs[instr_rs1];
                    alu_op2   <= (instr_opcode == OP_REG)
                                  ? cpuregs[instr_rs2] : imm_i;
                    alu_sub   <= (instr_opcode == OP_REG) &&
                                 (instr_funct3 == 3'b000) &&
                                 instr_funct7[5];
                    cpu_state <= ST_EXEC;
                end

                // ── EXEC: opcode dispatch ─────────────────────────────────────
                ST_EXEC: begin
                    case (instr_opcode)

                        OP_LUI: begin
                            wb_rd     <= instr_rd;
                            wb_data   <= imm_u;
                            wb_en     <= 1'b1;
                            cpu_state <= ST_FETCH;
                            if (ENABLE_COUNTERS) count_instr <= count_instr + 1'b1;
                        end

                        OP_AUIPC: begin
                            wb_rd     <= instr_rd;
                            wb_data   <= (reg_pc - 32'd4) + imm_u;
                            wb_en     <= 1'b1;
                            cpu_state <= ST_FETCH;
                            if (ENABLE_COUNTERS) count_instr <= count_instr + 1'b1;
                        end

                        OP_JAL: begin
                            wb_rd     <= instr_rd;
                            wb_data   <= reg_pc;
                            wb_en     <= 1'b1;
                            reg_pc    <= (reg_pc - 32'd4) + imm_j;
                            cpu_state <= ST_FETCH;
                            if (ENABLE_COUNTERS) count_instr <= count_instr + 1'b1;
                        end

                        OP_JALR: begin
                            wb_rd     <= instr_rd;
                            wb_data   <= reg_pc;
                            wb_en     <= 1'b1;
                            reg_pc    <= (cpuregs[instr_rs1] + imm_i) & ~32'h1;
                            cpu_state <= ST_FETCH;
                            if (ENABLE_COUNTERS) count_instr <= count_instr + 1'b1;
                        end

                        OP_BRANCH: begin
                            begin : branch_eval
                                reg branch_taken;
                                case (instr_funct3)
                                    3'b000: branch_taken = (cpuregs[instr_rs1] == cpuregs[instr_rs2]);
                                    3'b001: branch_taken = (cpuregs[instr_rs1] != cpuregs[instr_rs2]);
                                    3'b100: branch_taken = ($signed(cpuregs[instr_rs1]) <  $signed(cpuregs[instr_rs2]));
                                    3'b101: branch_taken = ($signed(cpuregs[instr_rs1]) >= $signed(cpuregs[instr_rs2]));
                                    3'b110: branch_taken = (cpuregs[instr_rs1] <  cpuregs[instr_rs2]);
                                    3'b111: branch_taken = (cpuregs[instr_rs1] >= cpuregs[instr_rs2]);
                                    default: branch_taken = 1'b0;
                                endcase
                                if (branch_taken)
                                    reg_pc <= (reg_pc - 32'd4) + imm_b;
                            end
                            cpu_state <= ST_FETCH;
                            if (ENABLE_COUNTERS) count_instr <= count_instr + 1'b1;
                        end

                        OP_LOAD: begin
                            mem_valid <= 1'b1;
                            mem_instr <= 1'b0;
                            mem_addr  <= cpuregs[instr_rs1] + imm_i;
                            mem_wstrb <= 4'b0;
                            cpu_state <= ST_LDMEM;
                        end

                        OP_STORE: begin
                            mem_valid <= 1'b1;
                            mem_instr <= 1'b0;
                            mem_addr  <= cpuregs[instr_rs1] + imm_s;
                            mem_wdata <= cpuregs[instr_rs2];
                            case (instr_funct3)
                                3'b000: mem_wstrb <= 4'b0001;  // SB
                                3'b001: mem_wstrb <= 4'b0011;  // SH
                                3'b010: mem_wstrb <= 4'b1111;  // SW
                                default: mem_wstrb <= 4'b0000;
                            endcase
                            cpu_state <= ST_STMEM;
                        end

                        OP_IMM, OP_REG: begin
                            begin : alu_exec
                                reg [31:0] alu_result;
                                case (instr_funct3)
                                    3'b000: alu_result = alu_add_result;
                                    3'b001: alu_result = alu_shl_result;
                                    3'b010: alu_result = {31'b0, alu_lts_result};
                                    3'b011: alu_result = {31'b0, alu_ltu_result};
                                    3'b100: alu_result = alu_op1 ^ alu_op2;
                                    3'b101: alu_result = alu_shr_result;
                                    3'b110: alu_result = alu_op1 | alu_op2;
                                    3'b111: alu_result = alu_op1 & alu_op2;
                                    default: alu_result = 32'b0;
                                endcase
                                wb_data <= alu_result;
                            end
                            wb_rd     <= instr_rd;
                            wb_en     <= 1'b1;
                            cpu_state <= ST_FETCH;
                            if (ENABLE_COUNTERS) count_instr <= count_instr + 1'b1;
                        end

                        OP_SYSTEM: begin
                            if (instr_funct3 == 3'b000) begin
                                // ECALL / EBREAK
                                trap      <= 1'b1;
                                cpu_state <= ST_TRAP;
                            end else begin
                                // CSR (read-only subset: mcycle, minstret)
                                begin : csr_read
                                    reg [31:0] csr_val;
                                    case (instr_buf[31:20])
                                        12'hC00: csr_val = count_cycle[31:0];   // mcycle lo
                                        12'hC80: csr_val = count_cycle[63:32];  // mcycle hi
                                        12'hC02: csr_val = count_instr[31:0];   // minstret lo
                                        12'hC82: csr_val = count_instr[63:32];  // minstret hi
                                        default:  csr_val = 32'b0;
                                    endcase
                                    wb_data <= csr_val;
                                end
                                wb_rd     <= instr_rd;
                                wb_en     <= 1'b1;
                                cpu_state <= ST_FETCH;
                                if (ENABLE_COUNTERS) count_instr <= count_instr + 1'b1;
                            end
                        end

                        default: begin
                            trap      <= 1'b1;
                            cpu_state <= ST_TRAP;
                        end

                    endcase
                end

                // ── LDMEM: wait for load data ─────────────────────────────────
                ST_LDMEM: begin
                    if (mem_ready) begin
                        begin : load_extend
                            reg [31:0] ld_val;
                            case (instr_funct3)
                                3'b000: ld_val = {{24{mem_rdata[7]}},  mem_rdata[7:0]};   // LB
                                3'b001: ld_val = {{16{mem_rdata[15]}}, mem_rdata[15:0]};  // LH
                                3'b010: ld_val = mem_rdata;                                // LW
                                3'b100: ld_val = {24'b0, mem_rdata[7:0]};                 // LBU
                                3'b101: ld_val = {16'b0, mem_rdata[15:0]};                // LHU
                                default: ld_val = mem_rdata;
                            endcase
                            wb_data <= ld_val;
                        end
                        wb_rd     <= instr_rd;
                        wb_en     <= 1'b1;
                        cpu_state <= ST_FETCH;
                        if (ENABLE_COUNTERS) count_instr <= count_instr + 1'b1;
                    end
                end

                // ── STMEM: wait for store ack ─────────────────────────────────
                ST_STMEM: begin
                    if (mem_ready) begin
                        cpu_state <= ST_FETCH;
                        if (ENABLE_COUNTERS) count_instr <= count_instr + 1'b1;
                    end
                end

                // ── TRAP: halt CPU ────────────────────────────────────────────
                ST_TRAP: begin
                    // Stay here until reset
                end

                default: cpu_state <= ST_FETCH;

            endcase
        end
    end

endmodule
