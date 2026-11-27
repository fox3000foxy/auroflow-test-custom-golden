
#!/usr/bin/env bash
set -euo pipefail

R="${VM_ROOT:-$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)}"

ISO="${WINDOWS_ISO:-$R/windows-install.iso}"

FIRMWARE="${OVMF_FIRMWARE:-/usr/share/OVMF/OVMF_CODE_4M.ms.fd}"

RAM=$(( ($(awk '/MemAvailable:/ {print int($2/1024)}' /proc/meminfo)-1024)/256*256 ))
CPU="${VM_CPU:-$(nproc)}"
RAM="${VM_RAM:-$RAM}"

test "$RAM" -ge 4096
echo "[VM] Allocated RAM: ${RAM} MiB"
echo "[VM] Allocated vCPUs: ${CPU}"
test -f "$FIRMWARE"
test -f "$R/disk.qcow2"
test -f "$R/OVMF_VARS.fd"

UUID=$(cat "$R/uuid")
if [[ ! "$UUID" =~ ^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$ ]]; then
    cat /proc/sys/kernel/random/uuid > "$R/uuid"
    UUID=$(cat "$R/uuid")
fi
MAC=$(cat "$R/mac")

if [[ -f "$R/tpm.pid" ]] && \
   kill -0 "$(cat "$R/tpm.pid")" 2>/dev/null; then
    echo "Un processus TPM est déjà actif."
    exit 1
fi

rm -f "$R/tpm.sock" "$R/tpm.pid" "$R/tpm-stopped" "$R/clean"

swtpm socket \
  --tpm2 \
  --tpmstate dir="$R/tpm" \
  --ctrl type=unixio,path="$R/tpm.sock" \
  --pid file="$R/tpm.pid" \
  --daemon

cleanup() {
    local pid=""

    if [[ -f "$R/tpm.pid" ]]; then
        pid=$(cat "$R/tpm.pid")
    fi

    if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
        kill -TERM "$pid" 2>/dev/null || true

        for i in $(seq 1 100); do
            if ! kill -0 "$pid" 2>/dev/null ||
               [[ "$(ps -o stat= -p "$pid" 2>/dev/null)" == Z* ]]; then
                break
            fi
            sleep 0.1
        done

        if kill -0 "$pid" 2>/dev/null &&
           [[ "$(ps -o stat= -p "$pid" 2>/dev/null)" != Z* ]]; then
            echo "[TPM] Shutdown timeout" >&2
            return 1
        fi
    fi

    rm -f "$R/tpm.pid" "$R/tpm.sock"
    touch "$R/tpm-stopped"
    echo "[TPM] Shutdown confirmed"
}

trap cleanup EXIT

args=()

for i in $(seq 1 14); do
    slot=$((2+(i-1)/8))
    fn=$(((i-1)%8))
    multi=''

    if [ "$fn" = 0 ]; then
        multi=',multifunction=on'
    fi

    args+=(
      -device "pcie-root-port,id=pci.$i,bus=pcie.0,addr=$slot.$fn,chassis=$i,port=$((15+i))$multi"
    )
done

media=()

if [[ "${1:-}" == "--install" ]]; then
    test -f "$ISO"
    test -f "$R/virtio-win.iso"

    media+=(
      -drive "file=$ISO,format=raw,if=none,media=cdrom,id=windowsiso,readonly=on"
      -device "ide-cd,drive=windowsiso,bus=ide.1,id=sata0-0-1,bootindex=1"
      -drive "file=$R/virtio-win.iso,format=raw,if=none,media=cdrom,id=virtioiso,readonly=on"
      -device "ide-cd,drive=virtioiso,bus=ide.2"
    )
else
    media+=(
      -device ide-cd,bus=ide.1,id=sata0-0-1
    )
fi

qemu-system-x86_64 \
  -name windows10-iot-ltsc \
  -uuid "$UUID" \
  -machine pc-q35-10.0,usb=off,smm=on,hpet=off,acpi=on \
  -accel kvm \
  -cpu host,migratable=on,hv-time=on,hv-relaxed=on,hv-vapic=on,hv-spinlocks=0x1fff,hv-vpindex=on,hv-runtime=on,hv-synic=on,hv-stimer=on,hv-frequencies=on,hv-tlbflush=on,hv-ipi=on \
  -smp "$CPU",sockets="$CPU",cores=1,threads=1 \
  -m "$RAM" \
  -no-user-config \
  -no-shutdown \
  -nodefaults \
  -rtc base=localtime,driftfix=slew \
  -global kvm-pit.lost_tick_policy=delay \
  -global ICH9-LPC.disable_s3=1 \
  -global ICH9-LPC.disable_s4=1 \
  -global ICH9-LPC.noreboot=off \
  -global driver=cfi.pflash01,property=secure,value=on \
  -boot strict=on,menu=on,splash-time=10000 \
  -smbios 'type=0,vendor=American Megatrends Inc.,version=P2.50,date=07/12/2018' \
  -smbios 'type=1,manufacturer=ASRock,product=B250 Pro4' \
  -smbios 'type=2,manufacturer=ASRock,product=B250 Pro4' \
  -drive "if=pflash,format=raw,unit=0,readonly=on,file=$FIRMWARE" \
  -drive "if=pflash,format=raw,unit=1,file=$R/OVMF_VARS.fd" \
  "${args[@]}" \
  -device qemu-xhci,id=usb,p2=15,p3=15,bus=pci.2,addr=0x0 \
  -device virtio-scsi-pci,id=scsi0,bus=pci.4,addr=0x0 \
  -drive "file=$R/disk.qcow2,format=qcow2,if=none,id=systemdisk,discard=unmap,detect-zeroes=unmap" \
  -device 'scsi-hd,drive=systemdisk,id=scsi0-0-0-0,device_id=drive-scsi0-0-0-0,bus=scsi0.0,channel=0,scsi-id=0,lun=0,bootindex=2,vendor=SanDisk,product=SSD PLUS 64GB' \
  "${media[@]}" \
  -device virtio-balloon-pci,id=balloon0,bus=pci.3,addr=0x0 \
  -device usb-tablet,id=input0,bus=usb.0,port=1 \
  -chardev "socket,id=chrtpm,path=$R/tpm.sock" \
  -tpmdev emulator,id=tpm-tpm0,chardev=chrtpm \
  -device tpm-crb,tpmdev=tpm-tpm0,id=tpm0 \
  -netdev user,id=hostnet0,hostfwd=tcp:127.0.0.1:3390-:3389 \
  -device "virtio-net-pci,netdev=hostnet0,id=net0,mac=$MAC,bus=pci.1,addr=0x0" \
  -audiodev none,id=audio1 \
  -device VGA,id=video0,vgamem_mb=16,bus=pcie.0,addr=0x1 \
  -display none \
  -pidfile "$R/qemu.pid" \
  -qmp "unix:$R/qmp.sock,server=on,wait=off"
