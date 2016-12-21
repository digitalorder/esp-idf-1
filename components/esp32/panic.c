// Copyright 2015-2016 Espressif Systems (Shanghai) PTE LTD
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at

//     http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.
#include <stdlib.h>

#include <xtensa/config/core.h>

#include "rom/rtc.h"

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/xtensa_api.h"

#include "soc/uart_reg.h"
#include "soc/io_mux_reg.h"
#include "soc/dport_reg.h"
#include "soc/rtc_cntl_reg.h"
#include "soc/timer_group_struct.h"
#include "soc/timer_group_reg.h"
#include "soc/cpu.h"

#include "esp_gdbstub.h"
#include "esp_panic.h"
#include "esp_attr.h"
#include "esp_err.h"
#include "esp_core_dump.h"

/*
  Panic handlers; these get called when an unhandled exception occurs or the assembly-level
  task switching / interrupt code runs into an unrecoverable error. The default task stack
  overflow handler and abort handler are also in here.
*/

/*
  Note: The linker script will put everything in this file in IRAM/DRAM, so it also works with flash cache disabled.
*/

#if !CONFIG_ESP32_PANIC_SILENT_REBOOT
//printf may be broken, so we fix our own printing fns...
void esp_panicPutChar(char c)
{
    while (((READ_PERI_REG(UART_STATUS_REG(0)) >> UART_TXFIFO_CNT_S)&UART_TXFIFO_CNT) >= 126) ;
    WRITE_PERI_REG(UART_FIFO_REG(0), c);
}

void esp_panicPutStr(const char *c)
{
    int x = 0;
    while (c[x] != 0) {
        esp_panicPutChar(c[x]);
        x++;
    }
}

void esp_panicPutHex(int a)
{
    int x;
    int c;
    for (x = 0; x < 8; x++) {
        c = (a >> 28) & 0xf;
        if (c < 10) {
            esp_panicPutChar('0' + c);
        } else {
            esp_panicPutChar('a' + c - 10);
        }
        a <<= 4;
    }
}

void esp_panicPutDec(int a)
{
    int n1, n2;
    n1 = a % 10;
    n2 = a / 10;
    if (n2 == 0) {
        esp_panicPutChar(' ');
    } else {
        esp_panicPutChar(n2 + '0');
    }
    esp_panicPutChar(n1 + '0');
}
#else
//No printing wanted. Stub out these functions.
void esp_panicPutChar(char c) { }
void esp_panicPutStr(const char *c) { }
void esp_panicPutHex(int a) { }
void esp_panicPutDec(int a) { }
#endif

void  __attribute__((weak)) vApplicationStackOverflowHook( TaskHandle_t xTask, signed char *pcTaskName )
{
    esp_panicPutStr("***ERROR*** A stack overflow in task ");
    esp_panicPutStr((char *)pcTaskName);
    esp_panicPutStr(" has been detected.\r\n");
    abort();
}

static bool abort_called;

void abort()
{
#if !CONFIG_ESP32_PANIC_SILENT_REBOOT
    ets_printf("abort() was called at PC 0x%08x\n", (intptr_t)__builtin_return_address(0) - 3);
#endif
    abort_called = true;
    while(1) {
        __asm__ ("break 0,0");
        *((int*) 0) = 0;
    }
}


static const char *edesc[] = {
    "IllegalInstruction", "Syscall", "InstructionFetchError", "LoadStoreError",
    "Level1Interrupt", "Alloca", "IntegerDivideByZero", "PCValue",
    "Privileged", "LoadStoreAlignment", "res", "res",
    "InstrPDAddrError", "LoadStorePIFDataError", "InstrPIFAddrError", "LoadStorePIFAddrError",
    "InstTLBMiss", "InstTLBMultiHit", "InstFetchPrivilege", "res",
    "InstrFetchProhibited", "res", "res", "res",
    "LoadStoreTLBMiss", "LoadStoreTLBMultihit", "LoadStorePrivilege", "res",
    "LoadProhibited", "StoreProhibited", "res", "res",
    "Cp0Dis", "Cp1Dis", "Cp2Dis", "Cp3Dis",
    "Cp4Dis", "Cp5Dis", "Cp6Dis", "Cp7Dis"
};


static void commonErrorHandler(XtExcFrame *frame);

//The fact that we've panic'ed probably means the other CPU is now running wild, possibly
//messing up the serial output, so we stall it here.
static void haltOtherCore()
{
    esp_cpu_stall( xPortGetCoreID() == 0 ? 1 : 0 );
}

void panicHandler(XtExcFrame *frame)
{
    int *regs = (int *)frame;
    //Please keep in sync with PANIC_RSN_* defines
    const char *reasons[] = {
        "Unknown reason",
        "Unhandled debug exception",
        "Double exception",
        "Unhandled kernel exception",
        "Coprocessor exception",
        "Interrupt wdt timeout on CPU0",
        "Interrupt wdt timeout on CPU1",
    };
    const char *reason = reasons[0];
    //The panic reason is stored in the EXCCAUSE register.
    if (regs[20] <= PANIC_RSN_MAX) {
        reason = reasons[regs[20]];
    }
    haltOtherCore();
    esp_panicPutStr("Guru Meditation Error: Core ");
    esp_panicPutDec(xPortGetCoreID());
    esp_panicPutStr(" panic'ed (");
    if (!abort_called) {
        esp_panicPutStr(reason);
        esp_panicPutStr(")\r\n");
            if (regs[20]==PANIC_RSN_DEBUGEXCEPTION) {
                int debugRsn;
                asm("rsr.debugcause %0":"=r"(debugRsn));
                esp_panicPutStr("Debug exception reason: ");
                if (debugRsn&XCHAL_DEBUGCAUSE_ICOUNT_MASK) esp_panicPutStr("SingleStep ");
                if (debugRsn&XCHAL_DEBUGCAUSE_IBREAK_MASK) esp_panicPutStr("HwBreakpoint ");
                if (debugRsn&XCHAL_DEBUGCAUSE_DBREAK_MASK) {
                    //Unlike what the ISA manual says, this core seemingly distinguishes from a DBREAK
                    //reason caused by watchdog 0 and one caused by watchdog 1 by setting bit 8 of the
                    //debugcause if the cause is watchdog 1 and clearing it if it's watchdog 0.
                    if (debugRsn&(1<<8)) {
#if CONFIG_FREERTOS_WATCHPOINT_END_OF_STACK
                        esp_panicPutStr("Stack canary watchpoint triggered ");
#else
                        esp_panicPutStr("Watchpoint 1 triggered ");
#endif
                    } else {
                        esp_panicPutStr("Watchpoint 0 triggered ");
                    }
                }
                if (debugRsn&XCHAL_DEBUGCAUSE_BREAK_MASK) esp_panicPutStr("BREAK instr ");
                if (debugRsn&XCHAL_DEBUGCAUSE_BREAKN_MASK) esp_panicPutStr("BREAKN instr ");
                if (debugRsn&XCHAL_DEBUGCAUSE_DEBUGINT_MASK) esp_panicPutStr("DebugIntr ");
                esp_panicPutStr("\r\n");
            }
        } else {
            esp_panicPutStr("abort)\r\n");
        }

    if (esp_cpu_in_ocd_debug_mode()) {
        asm("break.n 1");
    }
    commonErrorHandler(frame);
}

static void setFirstBreakpoint(uint32_t pc)
{
    asm(
        "wsr.ibreaka0 %0\n" \
        "rsr.ibreakenable a3\n" \
        "movi a4,1\n" \
        "or a4, a4, a3\n" \
        "wsr.ibreakenable a4\n" \
        ::"r"(pc):"a3", "a4");
}

void xt_unhandled_exception(XtExcFrame *frame)
{
    int *regs = (int *)frame;
    int x;

    haltOtherCore();
    esp_panicPutStr("Guru Meditation Error of type ");
    x = regs[20];
    if (x < 40) {
        esp_panicPutStr(edesc[x]);
    } else {
        esp_panicPutStr("Unknown");
    }
    esp_panicPutStr(" occurred on core ");
    esp_panicPutDec(xPortGetCoreID());
    if (esp_cpu_in_ocd_debug_mode()) {
        esp_panicPutStr(" at pc=");
        esp_panicPutHex(regs[1]);
        esp_panicPutStr(". Setting bp and returning..\r\n");
        //Stick a hardware breakpoint on the address the handler returns to. This way, the OCD debugger
        //will kick in exactly at the context the error happened.
        setFirstBreakpoint(regs[1]);
        return;
    }
    esp_panicPutStr(". Exception was unhandled.\r\n");
    commonErrorHandler(frame);
}


/*
  If watchdogs are enabled, the panic handler runs the risk of getting aborted pre-emptively because
  an overzealous watchdog decides to reset it. On the other hand, if we disable all watchdogs, we run
  the risk of somehow halting in the panic handler and not resetting. That is why this routine kills
  all watchdogs except the timer group 0 watchdog, and it reconfigures that to reset the chip after
  one second.
*/
static void reconfigureAllWdts()
{
    TIMERG0.wdt_wprotect = TIMG_WDT_WKEY_VALUE;
    TIMERG0.wdt_feed = 1;
    TIMERG0.wdt_config0.sys_reset_length = 7;           //3.2uS
    TIMERG0.wdt_config0.cpu_reset_length = 7;           //3.2uS
    TIMERG0.wdt_config0.stg0 = TIMG_WDT_STG_SEL_RESET_SYSTEM; //1st stage timeout: reset system
    TIMERG0.wdt_config1.clk_prescale = 80 * 500;        //Prescaler: wdt counts in ticks of 0.5mS
    TIMERG0.wdt_config2 = 2000;                         //1 second before reset
    TIMERG0.wdt_config0.en = 1;
    TIMERG0.wdt_wprotect = 0;
    //Disable wdt 1
    TIMERG1.wdt_wprotect = TIMG_WDT_WKEY_VALUE;
    TIMERG1.wdt_config0.en = 0;
    TIMERG1.wdt_wprotect = 0;
}

#if CONFIG_ESP32_PANIC_GDBSTUB || CONFIG_ESP32_PANIC_PRINT_HALT
/*
  This disables all the watchdogs for when we call the gdbstub.
*/
static void disableAllWdts()
{
    TIMERG0.wdt_wprotect = TIMG_WDT_WKEY_VALUE;
    TIMERG0.wdt_config0.en = 0;
    TIMERG0.wdt_wprotect = 0;
    TIMERG1.wdt_wprotect = TIMG_WDT_WKEY_VALUE;
    TIMERG1.wdt_config0.en = 0;
    TIMERG0.wdt_wprotect = 0;
}

#endif

static inline bool stackPointerIsSane(uint32_t sp)
{
    return !(sp < 0x3ffae010 || sp > 0x3ffffff0 || ((sp & 0xf) != 0));
}

static void putEntry(uint32_t pc, uint32_t sp)
{
    if (pc & 0x80000000) {
        pc = (pc & 0x3fffffff) | 0x40000000;
    }
    esp_panicPutStr(" 0x");
    esp_panicPutHex(pc);
    esp_panicPutStr(":0x");
    esp_panicPutHex(sp);
}

static void doBacktrace(XtExcFrame *frame)
{
    uint32_t i = 0, pc = frame->pc, sp = frame->a1;
    esp_panicPutStr("\r\nBacktrace:");
    /* Do not check sanity on first entry, PC could be smashed. */
    putEntry(pc, sp);
    pc = frame->a0;
    while (i++ < 100) {
        uint32_t psp = sp;
        if (!stackPointerIsSane(sp) || i++ > 100) {
            break;
        }
        sp = *((uint32_t *) (sp - 0x10 + 4));
        putEntry(pc, sp);
        pc = *((uint32_t *) (psp - 0x10));
        if (pc < 0x40000000) {
            break;
        }
    }
    esp_panicPutStr("\r\n\r\n");
}

/*
  We arrive here after a panic or unhandled exception, when no OCD is detected. Dump the registers to the
  serial port and either jump to the gdb stub, halt the CPU or reboot.
*/
static void commonErrorHandler(XtExcFrame *frame)
{
    int *regs = (int *)frame;
    int x, y;
    const char *sdesc[] = {
        "PC      ", "PS      ", "A0      ", "A1      ", "A2      ", "A3      ", "A4      ", "A5      ",
        "A6      ", "A7      ", "A8      ", "A9      ", "A10     ", "A11     ", "A12     ", "A13     ",
        "A14     ", "A15     ", "SAR     ", "EXCCAUSE", "EXCVADDR", "LBEG    ", "LEND    ", "LCOUNT  "
    };

    //Feed the watchdogs, so they will give us time to print out debug info
    reconfigureAllWdts();

    /* only dump registers for 'real' crashes, if crashing via abort()
       the register window is no longer useful.
    */
    if (!abort_called) {
        esp_panicPutStr("Register dump:\r\n");

        for (x = 0; x < 24; x += 4) {
            for (y = 0; y < 4; y++) {
                if (sdesc[x + y][0] != 0) {
                    esp_panicPutStr(sdesc[x + y]);
                    esp_panicPutStr(": 0x");
                    esp_panicPutHex(regs[x + y + 1]);
                    esp_panicPutStr("  ");
                }
                esp_panicPutStr("\r\n");
            }
        }
    }

    /* With windowed ABI backtracing is easy, let's do it. */
    doBacktrace(frame);

#if CONFIG_ESP32_PANIC_GDBSTUB
    disableAllWdts();
    esp_panicPutStr("Entering gdb stub now.\r\n");
    esp_gdbstub_panic_handler(frame);
#else
#if CONFIG_ESP32_ENABLE_COREDUMP_TO_FLASH
    esp_core_dump_to_flash(frame);
#endif
#if CONFIG_ESP32_ENABLE_COREDUMP_TO_UART && !CONFIG_ESP32_PANIC_SILENT_REBOOT
    esp_core_dump_to_uart(frame);
#endif
#if CONFIG_ESP32_PANIC_PRINT_REBOOT || CONFIG_ESP32_PANIC_SILENT_REBOOT
    esp_panicPutStr("Rebooting...\r\n");
    for (x = 0; x < 100; x++) {
        ets_delay_us(1000);
    }
    software_reset();
#else
    disableAllWdts();
    esp_panicPutStr("CPU halted.\r\n");
    while (1);
#endif
#endif
}


void esp_set_breakpoint_if_jtag(void *fn)
{
    if (esp_cpu_in_ocd_debug_mode()) {
        setFirstBreakpoint((uint32_t)fn);
    }
}


esp_err_t esp_set_watchpoint(int no, void *adr, int size, int flags)
{
    int x;
    if (no<0 || no>1) return ESP_ERR_INVALID_ARG;
    if (flags&(~0xC0000000)) return ESP_ERR_INVALID_ARG;
    int dbreakc=0x3F;
    //We support watching 2^n byte values, from 1 to 64. Calculate the mask for that.
    for (x=0; x<7; x++) {
        if (size==(1<<x)) break;
        dbreakc<<=1;
    }
    if (x==7) return ESP_ERR_INVALID_ARG;
    //Mask mask and add in flags.
    dbreakc=(dbreakc&0x3f)|flags;

    if (no==0) {
        asm volatile(
            "wsr.dbreaka0 %0\n" \
            "wsr.dbreakc0 %1\n" \
            ::"r"(adr),"r"(dbreakc));
    } else {
        asm volatile(
            "wsr.dbreaka1 %0\n" \
            "wsr.dbreakc1 %1\n" \
            ::"r"(adr),"r"(dbreakc));
    }
    return ESP_OK;
}

void esp_clear_watchpoint(int no)
{
    //Setting a dbreakc register to 0 makes it trigger on neither load nor store, effectively disabling it.
    int dbreakc=0;
    if (no==0) {
        asm volatile(
            "wsr.dbreakc0 %0\n" \
            ::"r"(dbreakc));
    } else {
        asm volatile(
            "wsr.dbreakc1 %0\n" \
            ::"r"(dbreakc));
    }
}


