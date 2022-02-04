#!/bin/sh

M="e2_procfs"

cp -fv pre/$M.conf /lib/modules-load.d
cd pre/$M
make -C /lib/modules/`uname -r`/build M=`pwd`
cp -fv $M.ko /lib/modules/`uname -r`/kernel/drivers/media/common
make clean
cd ../..
depmod -a