"""This module parses the xml provided by nvidia-smi to obtain the consumption, memory and SM used for each gpu and each pid."""

from subprocess import PIPE, Popen
import re
import subprocess
from collections import OrderedDict
from io import StringIO
from xml.etree.ElementTree import fromstring
from shutil import which
import numpy as np
import pandas as pd

def is_nvidia_compatible():
    """
    return boolean corresponding to checking Check if this system supports nvidia tools required
    """

    msg = "nvidia NOT available for energy consumption\n"
    if which("nvidia-smi") is None:
        return (False, msg+"nvidia-smi program is not in the path")
    # make sure that nvidia-smi doesn't just return no devices
    p = Popen(["nvidia-smi"], stdout=PIPE)
    stdout, _ = p.communicate()
    output = stdout.decode("UTF-8")
    if "no devices" in output.lower():
        return (False, msg+"nvidia-smi did not found GPU device on this machine")
    if "NVIDIA-SMI has failed".lower() in output.lower():
        return (False, msg+output)
    xml = get_nvidia_xml()
    for _, gpu in enumerate(xml.findall("gpu")):
        try:
            get_gpu_data(gpu)
        except RuntimeError as e:
            return (False, msg+e.__str__())
        break
    msg = "GPU power will be measured with nvidia"
    return True, msg

def get_gpu_use_pmon(nsample=1):
    """
    Find per process per gpu usage info
     -c corresponds to the number of samples
     according to the docs, this command is limited to 4 gpus
     information includes the pid, command name and       average utilization values for SM (streaming multiprocessor), Memory, Encoder  and  Decoder  since       the  last  monitoring  cycle
     result is a panda frame with the following columns
     gpu   pid    sm   mem  enc  dec
    """
    sp = subprocess.Popen(
        ["nvidia-smi", "pmon", "-c", str(nsample)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    out_str = sp.communicate()

    # now doing some processing to extract the info from the command output string
    out_str_split = out_str[0].decode("utf-8").split("\n")
    # sometimes with too many processess on the machine or too many gpus, this command will reprint the headers
    # to avoid that we just remove duplicate lines
    out_str_split = list(OrderedDict.fromkeys(out_str_split))
    # remove the idx line
    out_str_pruned = [
        x for x in out_str_split if "Idx" not in x
    ]  # [out_str_split[0], ] + out_str_split[2:]

    # For some weird reason the header position sometimes gets jumbled so we need to re-order it to the front
    position = -1

    for i, x in enumerate(out_str_pruned):
        if re.match(r"#.* gpu ", x):
            position = i
    if position == -1:
        raise ValueError("Problem with output in nvidia-smi pmon -c 10")
    out_str_pruned.insert(0, out_str_pruned.pop(position).strip())
    out_str_final = "\n".join(out_str_pruned)
    out_str_final = out_str_final.replace("-", "0")
    out_str_final = out_str_final.replace("# ", "")
    out_str_final = re.sub(
        "  +", "\t", out_str_final
    )  # commands may have single spaces
    out_str_final = re.sub("\n\t", "\n", out_str_final)  # remove preceding space
    out_str_final = re.sub(r"\s+\n", "\n", out_str_final)  # else pd will mis-align
    out_str_final = out_str_final.strip()
    df = pd.read_csv(StringIO(out_str_final), engine="python", delimiter="\t")
    process_percentage_used_gpu = df.groupby(["gpu", "pid"]).mean().reset_index()
    return  process_percentage_used_gpu

def get_gpu_mem(gpu):
    """Get the gpu memory usage from one gpu"""
    memory_usage = gpu.findall("fb_memory_usage")[0]
    total_memory = memory_usage.findall("total")[0].text
    used_memory = memory_usage.findall("used")[0].text
    free_memory = memory_usage.findall("free")[0].text
    return {
        "total": total_memory,
        "used_memory": used_memory,
        "free_memory": free_memory,
    }

def get_gpu_use(gpu):
    """Get gpu utilization and memory usage"""
    utilization = gpu.findall("utilization")[0]
    gpu_util = utilization.findall("gpu_util")[0].text
    memory_util = utilization.findall("memory_util")[0].text
    return {"gpu_util": gpu_util, "memory_util": memory_util}

def get_gpu_power(gpu):
    """get the power draw for this gpu"""
    power_readings = gpu.findall("power_readings")[0]
    power_draw = power_readings.findall("power_draw")[0].text
    if power_draw  == 'N/A':
        raise RuntimeError("nvidia-smi could not retrieve power draw from the nvidia card. Check that it is supported on your hardware ?")
    power_draw = float(power_draw.replace("W", ""))
    return {"power_draw": power_draw}

def get_gpu_data(gpu):
    """get consumption, SM and memory use for one gpu

    Args:
        gpu: xml part regarding one specific gpu
    """
    gpu_data = {}
    name = gpu.findall("product_name")[0].text
    gpu_data["name"] = name
    gpu_data["memory"] = get_gpu_mem(gpu)
    gpu_data["utilization"] = get_gpu_use(gpu)
    gpu_data["power_readings"] = get_gpu_power(gpu)
    return gpu_data

def get_nvidia_xml():
    """Call nvidia-smi program to obtain the details about the GPUs"""
    p = subprocess.Popen(["nvidia-smi", "-q", "-x"], stdout=subprocess.PIPE)
    outs, _ = p.communicate()
    xml = fromstring(outs)
    return xml

def get_nvidia_gpu_power(pid_list, nsample = 1):
    """Get the power and use of each GPU.
    first, get gpu usage per process
    second get the power use of nvidia for each GPU
    then for each gpu and each process in pid_list compute its attributatble
    power

    Args:
        pid_list : list of processes to be measured

        nsample : number of queries to nvidia

    """
    process_percentage_used_gpu = get_gpu_use_pmon(nsample=nsample)

    # this commmand provides the full xml output
    xml = get_nvidia_xml()
    power = 0
    per_gpu_absolute_percent_usage = {}
    per_gpu_relative_percent_usage = {}
    absolute_power = 0
    per_gpu_performance_states = {}
    per_gpu_power_draw = {}
    per_gpu_mem_use = {}

    # for each gpu
    #    for each pid
    #        collect the amount of power the process's pid is consuming
    for gpu_id, gpu in enumerate(xml.findall("gpu")):
        gpu_data = {}
        per_gpu_mem_use[gpu_id] = {}

        gpu_data = get_gpu_data(gpu)

        per_gpu_power_draw[gpu_id] = gpu_data["power_readings"]["power_draw"] # power_this_gpu
        absolute_power += per_gpu_power_draw[gpu_id]

        # processes
        processes = gpu.findall("processes")[0]

        # all the info for processes on this particular gpu that we're on
        gpu_based_processes = process_percentage_used_gpu[
            process_percentage_used_gpu["gpu"] == gpu_id
        ]
        # what's the total absolute SM for this gpu across all accessible processes
        percentage_of_gpu_used_by_all_processes = float(gpu_based_processes["sm"].sum())
        for info in processes.findall("process_info"):
            pid = info.findall("pid")[0].text
            used_memory = info.findall("used_memory")[0].text
            sm_absolute_percent = gpu_based_processes[
                gpu_based_processes["pid"] == int(pid)
            ]["sm"].sum()
            if percentage_of_gpu_used_by_all_processes == 0:
                # avoid divide by zero, sometimes nothing is used so 0/0 should = 0 in this case
                sm_relative_percent = 0
            else:
                sm_relative_percent = (
                    sm_absolute_percent / percentage_of_gpu_used_by_all_processes
                )

            if int(pid) in pid_list:
                per_gpu_mem_use[gpu_id][int(pid)] = int(used_memory.replace('MiB',''))*1048576 # convert from mbytes to bytes
                # only add a gpu to the list if it's being used by one of the processes. sometimes nvidia-smi seems to list all gpus available
                # even if they're not being used by our application, this is a problem in a slurm setting
                if gpu_id not in per_gpu_absolute_percent_usage:
                    # percentage_of_gpu_used_by_all_processes
                    per_gpu_absolute_percent_usage[gpu_id] = 0
                if gpu_id not in per_gpu_relative_percent_usage:
                    # percentage_of_gpu_used_by_all_processes
                    per_gpu_relative_percent_usage[gpu_id] = 0

                if gpu_id not in per_gpu_performance_states:
                    # we only log information for gpus that we're using, we've noticed that nvidia-smi will sometimes return information
                    # about all gpu's on a slurm cluster even if they're not assigned to a worker
                    performance_state = gpu.findall("performance_state")[0].text
                    per_gpu_performance_states[gpu_id] = performance_state

                power += sm_relative_percent * per_gpu_power_draw[gpu_id]
                # want a proportion value rather than percentage
                per_gpu_absolute_percent_usage[gpu_id] += sm_absolute_percent / 100.0
                per_gpu_relative_percent_usage[gpu_id] += sm_relative_percent

    if len(per_gpu_absolute_percent_usage.values()) == 0:
        average_gpu_utilization = 0
        average_gpu_relative_utilization = 0
    else:
        average_gpu_utilization = np.mean(list(per_gpu_absolute_percent_usage.values()))
        average_gpu_relative_utilization = np.mean(
            list(per_gpu_relative_percent_usage.values())
        )
    per_gpu_average_estimated_utilization_absolute = []
    for _, row in process_percentage_used_gpu.iterrows():
        d = dict([(k,float(row[k])) for k in process_percentage_used_gpu.columns] )
        per_gpu_average_estimated_utilization_absolute.append(d)
    data_return_values_with_headers = {
        "nvidia_draw_absolute": absolute_power,
        "per_gpu_attributable_mem_use": per_gpu_mem_use,
        "nvidia_estimated_attributable_power_draw": power,
        "average_gpu_estimated_utilization_absolute": average_gpu_utilization,
        "per_gpu_average_estimated_utilization_absolute": per_gpu_average_estimated_utilization_absolute,
        "average_gpu_estimated_utilization_relative": average_gpu_relative_utilization,
        "per_gpu_performance_state": per_gpu_performance_states,
        "per_gpu_power_draw": per_gpu_power_draw,
    }
    return data_return_values_with_headers
