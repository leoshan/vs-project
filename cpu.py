import time
import os
import sys
import multiprocessing # 替换 threading
import psutil # 需要安装: pip install psutil

# --- 目标配置 ---
TARGET_OVERALL_CPU_PERCENT = 33.0
TARGET_MEMORY_PERCENT = 50.0     # 目标在您的日志中似乎已调整为50%
ADJUST_INTERVAL_SECONDS = 2.0
CPU_WORKER_CYCLE_PERIOD = 0.1
# --- CPU 控制全局变量 ---
# 对于多进程，需要使用 multiprocessing 提供的同步原语
# 使用 Value 来在进程间共享占空比，'d' 表示双精度浮点数
shared_duty_cycle_value = multiprocessing.Value('d', TARGET_OVERALL_CPU_PERCENT / 100.0)
duty_cycle_mp_lock = multiprocessing.Lock() #进程锁
cpu_worker_processes = [] # 从线程列表变为进程列表
stop_all_processes_event = multiprocessing.Event() # 进程事件

# --- CPU比例控制器增益 ---
KP_CPU = 0.4  # 可以尝试稍微增大一点增益，因为现在进程更能影响CPU
              # 之前的0.3或0.5也可以，如果0.4过快或过慢，再调整

class CpuWorkerProcess(multiprocessing.Process): # 继承自 Process
    """CPU工作进程，根据共享占空比执行计算和休眠。"""
    def __init__(self, name, duty_cycle_val, duty_lock, stop_event, cycle_period):
        super().__init__(name=name)
        # daemon=True 对于进程来说，意味着如果父进程退出，这些子进程会被终止
        # 这通常是我们期望的行为
        self.daemon = True
        self.duty_cycle_val = duty_cycle_val
        self.duty_lock = duty_lock
        self.stop_event = stop_event
        self.cycle_period = cycle_period

    def run(self):
        # print(f"{self.name} (PID: {os.getpid()}) started.") # 可以取消注释以查看进程ID
        try:
            while not self.stop_event.is_set():
                with self.duty_lock: # 使用进程锁
                    current_duty = self.duty_cycle_val.value

                start_time = time.perf_counter()
                work_target_end_time = start_time + (self.cycle_period * current_duty)
                cycle_end_time = start_time + self.cycle_period

                while time.perf_counter() < work_target_end_time:
                    if self.stop_event.is_set():
                        break
                    # 增加计算量，确保每个工作单元更"重"一些
                    _ = [x * x for x in range(30000)] # 从10000增加到30000
                
                if self.stop_event.is_set():
                    break

                sleep_duration = cycle_end_time - time.perf_counter()
                if sleep_duration > 0:
                    # 使用 Event.wait() 来实现可中断的休眠
                    interrupted = self.stop_event.wait(timeout=sleep_duration)
                    if interrupted: # 如果是被stop_event唤醒的
                        break
        except Exception as e:
            print(f"Exception in {self.name} (PID: {os.getpid()}): {e}")
        # finally: # 可以取消注释
            # print(f"{self.name} (PID: {os.getpid()}) stopped.")


def start_cpu_workers_mp(): # 重命名以区分
    """启动CPU工作进程。"""
    num_cores = os.cpu_count()
    if num_cores is None:
        print("警告: 无法确定CPU核心数量。默认启动1个工作进程。")
        num_cores = 1
    
    print(f"启动 {num_cores} 个CPU工作进程。")
    
    with duty_cycle_mp_lock:
        initial_duty = TARGET_OVERALL_CPU_PERCENT / 100.0
        shared_duty_cycle_value.value = max(0.0, min(1.0, initial_duty))

    for i in range(num_cores):
        worker = CpuWorkerProcess(name=f"CpuWorkerProcess-{i+1}",
                                  duty_cycle_val=shared_duty_cycle_value,
                                  duty_lock=duty_cycle_mp_lock,
                                  stop_event=stop_all_processes_event,
                                  cycle_period=CPU_WORKER_CYCLE_PERIOD)
        cpu_worker_processes.append(worker)
        worker.start()

# 内存分配函数保持不变
def allocate_memory_chunks(target_mb):
    if target_mb <= 0:
        return []
    allocated_memory_blocks = []
    block_size_bytes = 1024 * 1024
    bytes_to_allocate = int(target_mb * 1024 * 1024)
    allocated_bytes = 0
    try:
        while allocated_bytes < bytes_to_allocate:
            remaining_bytes = bytes_to_allocate - allocated_bytes
            current_block_size = min(block_size_bytes, remaining_bytes)
            if current_block_size <= 0: break
            block = bytearray(current_block_size)
            allocated_memory_blocks.append(block)
            allocated_bytes += current_block_size
        return allocated_memory_blocks
    except MemoryError:
        del allocated_memory_blocks
        return None
    except Exception:
        del allocated_memory_blocks
        return None

def main():
    # 全局变量的引用方式在 multiprocessing 中需要注意，这里我们直接用定义的 mp 对象
    allocated_mem_holder = []

    psutil.cpu_percent(interval=None) 
    time.sleep(0.1)

    start_cpu_workers_mp() # 调用新的启动函数
    
    current_process = psutil.Process(os.getpid()) # 主进程
    try:
        if psutil.WINDOWS:
            current_process.nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
        else:
            current_process.nice(10)
        print("主进程优先级已尝试降低。")
    except Exception as e:
        print(f"警告: 无法设置主进程优先级: {e}")

    try:
        while not stop_all_processes_event.is_set(): # 使用进程事件
            current_overall_cpu = psutil.cpu_percent(interval=ADJUST_INTERVAL_SECONDS)
            if current_overall_cpu is None:
                print("警告: psutil.cpu_percent 返回 None，跳过此周期。")
                time.sleep(ADJUST_INTERVAL_SECONDS)
                continue

            with duty_cycle_mp_lock: # 使用进程锁
                error_cpu = TARGET_OVERALL_CPU_PERCENT - current_overall_cpu
                adjustment = KP_CPU * (error_cpu / 100.0) 
                new_duty_cycle = shared_duty_cycle_value.value + adjustment # 从 Value 对象获取值
                shared_duty_cycle_value.value = max(0.0, min(1.0, new_duty_cycle)) # 设置 Value 对象的值
                current_actual_duty_cycle = shared_duty_cycle_value.value # 读取以供打印

            print(f"\n--- CPU 状态 ---")
            print(f"系统CPU: {current_overall_cpu:.2f}% (目标: {TARGET_OVERALL_CPU_PERCENT:.1f}%) | "
                  f"工作进程占空比: {current_actual_duty_cycle:.3f}")

            # --- 内存控制 (与之前相同) ---
            virtual_mem = psutil.virtual_memory()
            total_memory_bytes = virtual_mem.total
            system_used_bytes = virtual_mem.used
            python_rss_bytes = current_process.memory_info().rss # 这是主进程的RSS
            
            other_processes_mem_bytes = system_used_bytes - python_rss_bytes
            # 注意：子进程的RSS不会直接加到这里，psutil.virtual_memory().used 是系统级的
            # 我们的目标是让系统总内存达到 TARGET_MEMORY_PERCENT
            # Python 主进程需要调整其持有的内存，以协同达到这个目标。
            # 如果子进程也消耗大量内存（在这个脚本里它们主要消耗CPU），则这里的估算会更复杂。
            # 但由于子进程主要是CPU消耗，内存占用相对小，当前逻辑可以继续。
            if other_processes_mem_bytes < 0:
                other_processes_mem_bytes = 0 
            
            python_target_total_hold_bytes = (TARGET_MEMORY_PERCENT / 100.0) * total_memory_bytes - other_processes_mem_bytes
            current_script_allocated_bytes = sum(len(b) for b in allocated_mem_holder)
            bytes_to_change = python_target_total_hold_bytes - current_script_allocated_bytes
            
            print(f"--- 内存状态 ---")
            print(f"系统RAM: 总计 {total_memory_bytes/(1024**3):.2f}GB, "
                  f"已用 {system_used_bytes/(1024**3):.2f}GB ({virtual_mem.percent:.1f}%)")
            print(f"主Python进程 RSS: {python_rss_bytes/(1024**2):.2f}MB. " # 明确是主进程
                  f"脚本持有(主进程): {current_script_allocated_bytes/(1024**2):.2f}MB.")
            print(f"目标主Python进程持有约 {python_target_total_hold_bytes/(1024**2):.2f}MB "
                  f"以达到 {TARGET_MEMORY_PERCENT}% 系统内存使用率.")

            if bytes_to_change > 1 * 1024 * 1024:
                alloc_this_round_mb = min(bytes_to_change / (1024*1024), 256.0)
                # print(f"尝试额外分配 {alloc_this_round_mb:.2f} MB...") #减少重复打印
                new_blocks = allocate_memory_chunks(alloc_this_round_mb)
                if new_blocks is not None:
                    allocated_mem_holder.extend(new_blocks)
                    print(f"成功分配。脚本当前持有(主进程)约 {sum(len(b) for b in allocated_mem_holder)/(1024**2):.2f}MB.")
                else:
                    print("此轮内存分配失败 (可能系统内存不足)。")
            elif bytes_to_change < -1 * 1024 * 1024:
                bytes_to_release = abs(bytes_to_change)
                # print(f"需要释放约 {bytes_to_release/(1024**2):.2f} MB。") #减少重复打印
                released_bytes_count = 0
                while allocated_mem_holder and released_bytes_count < bytes_to_release:
                    block = allocated_mem_holder.pop()
                    released_bytes_count += len(block)
                print(f"已释放约 {released_bytes_count/(1024**2):.2f} MB。脚本当前持有(主进程)约 {sum(len(b) for b in allocated_mem_holder)/(1024**2):.2f}MB.")
            # else: #减少重复打印
                # print(f"内存分配接近目标或无需显著改变。")

    except KeyboardInterrupt:
        print("\n检测到Ctrl+C，正在退出...")
    finally:
        print("正在停止工作进程并清理资源...")
        stop_all_processes_event.set() # 通知所有进程停止
        for worker_proc in cpu_worker_processes:
            if worker_proc.is_alive():
                worker_proc.join(timeout=CPU_WORKER_CYCLE_PERIOD * 2 + 0.5) # 给点时间退出
                if worker_proc.is_alive(): # 如果超时后仍然存活，尝试终止
                    print(f"进程 {worker_proc.name} 未能在超时内退出，尝试终止...")
                    worker_proc.terminate() # 强制终止
                    worker_proc.join(timeout=1) # 等待终止完成


        if allocated_mem_holder:
            # print(f"释放主脚本持有的约 {sum(len(b) for b in allocated_mem_holder)/(1024**2):.2f}MB 内存。") #减少重复打印
            allocated_mem_holder.clear() 
        
        print("清理完成。程序退出。")

if __name__ == "__main__":
    # 在 Windows 上使用 multiprocessing 时，需要这个 __main__ 保护
    # 并且子进程会重新导入主模块，所以需要确保子进程不会再次执行 main() 中的初始化代码
    # (例如 start_cpu_workers_mp())。通过将启动代码放在 main() 中，
    # 并在 Windows 上由 if __name__ == "__main__": 保护是标准做法。
    # Linux 上通常不是问题，但保持这种结构是好的。
    multiprocessing.freeze_support() # 对Windows打包成exe时有用，其他情况无害

    if not hasattr(psutil, "cpu_percent") or \
       not hasattr(psutil, "virtual_memory") or \
       not hasattr(os, "cpu_count"):
        sys.exit("错误: psutil库功能缺失或 os.cpu_count 不可用。")
    
    main()
