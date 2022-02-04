#!/bin/sh

M="e2_procfs"

cp -fv pre/$M.conf /lib/modules-load.d
cd $M
make -C /lib/modules/`uname -r`/build M=`pwd`
cp -fv $M.ko /lib/modules/`uname -r`/kernel/drivers/media/dvb-frontends
depmod -a
make clean
