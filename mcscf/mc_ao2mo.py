#!/usr/bin/env python

import os
import ctypes
import tempfile
import numpy
import h5py
import pyscf.lib
import pyscf.lib.numpy_helper
import pyscf.ao2mo._ao2mo as _ao2mo

libmcscf = pyscf.lib.load_library('libmcscf')

# least memory requirements:
# nmo  ncore  ncas  outcore  incore
# 200  40     16    0.8GB    3.7 GB (_eri 1.6GB intermediates 1.3G)
# 250  50     16    1.7GB    8.2 GB (_eri 3.9GB intermediates 2.6G)
# 300  60     16    3.1GB    16.8GB (_eri 8.1GB intermediates 5.6G)
# 400  80     16    8.5GB    53  GB (_eri 25.6GB intermediates 19G)
# 500  100    16    19 GB
# 600  120    16    37 GB
# 750  150    16    85 GB



def trans_e1_incore(eri_ao, mo, ncore, ncas):
    nmo = mo.shape[1]
    nocc = ncore + ncas
    moji = numpy.array(numpy.hstack((mo,mo[:,:nocc])), order='F')
    ijshape = (nmo, nocc, 0, nmo)

    eri1 = _ao2mo.nr_e1_incore_(eri_ao, moji, ijshape)

    load_buf = lambda bufid: eri1[bufid*nmo:bufid*nmo+nmo]
    aapp, appa, Iapcv = \
            _trans_aapp_(mo, ncore, ncas, load_buf)
    jc_pp, kc_pp, Icvcv = \
            _trans_cvcv_(mo, ncore, ncas, load_buf)
    return jc_pp, kc_pp, aapp, appa, Iapcv, Icvcv


def trans_e1_outcore(casscf, mo, max_memory=None, ioblk_size=512, tmpdir=None,
                     verbose=0):
    mo = numpy.ascontiguousarray(mo)
    log = pyscf.lib.logger.Logger(casscf.stdout, verbose)
    mol = casscf.mol
    ncore = casscf.ncore
    ncas = casscf.ncas
    nao, nmo = mo.shape
    nocc = ncore + ncas
    moji = numpy.array(numpy.hstack((mo,mo[:,:nocc])), order='F')
    ijshape = (nmo, nocc, 0, nmo)

    nij_pair = nocc * nmo
    nao_pair = nao*(nao+1)/2
    mem_words, ioblk_words = \
            pyscf.ao2mo.direct._memory_and_ioblk_size(max_memory, ioblk_size,
                                                      nij_pair, nao_pair)
    e1_buflen = min(int(mem_words/nij_pair), nao_pair)
    ish_ranges = pyscf.ao2mo.outcore._info_sh_ranges(mol, e1_buflen)

    log.debug1('require disk %.8g MB, swap-block-shape (%d,%d), mem cache size %.8g MB',
               nij_pair*nao_pair*8/1e6, e1_buflen, nmo,
               max(e1_buflen*nij_pair,nmo*nao_pair)*8/1e6)

    swapfile = tempfile.NamedTemporaryFile(dir=tmpdir)
    fswap = h5py.File(swapfile.name)

    for istep, sh_range in enumerate(ish_ranges):
        log.debug1('step 1, AO %d:%d, [%d/%d], len(buf) = %d', \
                  sh_range[0], sh_range[1], istep+1, len(ish_ranges), \
                  sh_range[2])
        try:
            buf = _ao2mo.nr_e1range_(moji, sh_range, ijshape, \
                                     mol._atm, mol._bas, mol._env)
        except MemoryError:
            log.warn('not enough memory or limited virtual address space `ulimit -v`')
            raise MemoryError
        for ic in range(nocc):
            col0 = ic * nmo
            col1 = ic * nmo + nmo
            fswap['%d/%d'%(istep,ic)] = pyscf.lib.transpose(buf[:,col0:col1])
        buf = None

    ###########################
    def load_buf(bfn_id):
        buf = numpy.empty((nmo,nao_pair))
        col0 = 0
        for ic, _ in enumerate(ish_ranges):
            dat = fswap['%d/%d'%(ic,bfn_id)]
            col1 = col0 + dat.shape[1]
            buf[:nmo,col0:col1] = dat
            col0 = col1
        return buf
    aapp, appa, Iapcv = \
            _trans_aapp_(mo, ncore, ncas, load_buf)
    jc_pp, kc_pp, Icvcv = \
            _trans_cvcv_(mo, ncore, ncas, load_buf)
    fswap.close()
    return jc_pp, kc_pp, aapp, appa, Iapcv, Icvcv

def _trans_aapp_(mo, ncore, ncas, fload):
    nmo = mo.shape[1]
    nocc = ncore + ncas
    c_nmo = ctypes.c_int(nmo)
    funpack = pyscf.lib.numpy_helper._np_helper.NPdunpack_tril

    molk = numpy.asfortranarray(mo)
    klshape = (0, nmo, 0, nmo)

    japcv = numpy.empty((ncas,nmo,ncore,nmo-ncore))
    aapp = numpy.empty((ncas,ncas,nmo,nmo))
    appa = numpy.empty((ncas,nmo,nmo,ncas))
    ppp = numpy.empty((nmo,nmo,nmo))
    for i in range(ncas):
        buf = _ao2mo.nr_e2_(fload(ncore+i), molk, klshape)
        for j in range(nmo):
            funpack(c_nmo, buf[j].ctypes.data_as(ctypes.c_void_p),
                    ppp[j].ctypes.data_as(ctypes.c_void_p), ctypes.c_int(1))
        aapp[i] = ppp[ncore:nocc]
        appa[i] = ppp[:,:,ncore:nocc]
        #japcv = apcv * 4 - acpv.transpose(0,2,1,3) - avcp.transpose(0,3,2,1)
        libmcscf.MCSCFinplace_apcv(japcv[i].ctypes.data_as(ctypes.c_void_p),
                                   ppp.ctypes.data_as(ctypes.c_void_p),
                                   ctypes.c_int(ncore), ctypes.c_int(ncas),
                                   ctypes.c_int(nmo))
    return aapp, appa, japcv

def _trans_cvcv_(mo, ncore, ncas, fload):
    nmo = mo.shape[1]
    nocc = ncore + ncas
    c_nmo = ctypes.c_int(nmo)
    funpack = pyscf.lib.numpy_helper._np_helper.NPdunpack_tril

    molk = numpy.array(numpy.hstack((mo,mo[:,:nocc])), order='F')

    jc_pp = numpy.empty((ncore,nmo,nmo))
    kc_pp = numpy.empty((ncore,nmo,nmo))
    jcvcv = numpy.zeros((ncore,nmo-ncore,ncore,nmo-ncore))
    vcp = numpy.empty((nmo-ncore,ncore,nmo))
    cpp = numpy.empty((ncore,nmo,nmo))
    for i in range(ncore):
        buf = fload(i)
        klshape = (nmo, ncore, 0, nmo)
        _ao2mo.nr_e2_(buf[ncore:nmo], molk, klshape, vcp)
        kc_pp[i,ncore:] = vcp[:,i]

        klshape = (0, nmo, 0, nmo)
        _ao2mo.nr_e2_(buf[:ncore], molk, klshape, buf[:ncore])
        for j in range(ncore):
            funpack(c_nmo, buf[j].ctypes.data_as(ctypes.c_void_p),
                    cpp[j].ctypes.data_as(ctypes.c_void_p), ctypes.c_int(1))
        jc_pp[i] = cpp[i]
        kc_pp[i,:ncore] = cpp[:,i]

        #jcvcv = cvcv * 4 - cvcv.transpose(2,1,0,3) - ccvv.transpose(0,2,1,3)
        libmcscf.MCSCFinplace_cvcv(jcvcv[i].ctypes.data_as(ctypes.c_void_p),
                                   vcp.ctypes.data_as(ctypes.c_void_p),
                                   cpp.ctypes.data_as(ctypes.c_void_p),
                                   ctypes.c_int(ncore), ctypes.c_int(ncas),
                                   ctypes.c_int(nmo))
    return jc_pp, kc_pp, jcvcv



class _ERIS(object):
    def __init__(self, casscf, mo, method='incore'):
        mol = casscf.mol
        self.ncore = casscf.ncore
        self.ncas = casscf.ncas
        nmo = mo.shape[1]
        ncore = self.ncore
        ncas = self.ncas

        if method == 'outcore' \
           or _mem_usage(ncore, ncas, nmo)[0] + nmo**4*2/1e6 > casscf.max_memory*.9 \
           or casscf._scf._eri is None:
            self.jc_pp, self.kc_pp, \
            self.aapp, self.appa, \
            self.Iapcv, self.Icvcv = \
                    trans_e1_outcore(casscf, mo, max_memory=casscf.max_memory,
                                     verbose=casscf.verbose)
        elif method == 'incore' and casscf._scf._eri is not None:
            self.jc_pp, self.kc_pp, \
            self.aapp, self.appa, \
            self.Iapcv, self.Icvcv = \
                    trans_e1_incore(casscf._scf._eri, mo,
                                    casscf.ncore, casscf.ncas)
        else:
            raise KeyError('update ao2mo')

def _mem_usage(ncore, ncas, nmo):
    nvir = nmo - ncore
    outcore = (ncore**2*nvir**2 + ncas*nmo*ncore*nvir + ncore*nmo**2*3 +
               ncas**2*nmo**2*2 + nmo**3*2) * 8/1e6
    incore = outcore + nmo**4/1e6 + ncore*nmo**3*4/1e6
    if outcore > 10000:
        print 'Be careful with the virtual memorty address space `ulimit -v`'
    return incore, outcore

if __name__ == '__main__':
    from pyscf import scf
    from pyscf import gto
    from pyscf import ao2mo
    import mc1step

    mol = gto.Mole()
    mol.verbose = 0
    mol.output = None#"out_h2o"
    mol.atom = [
        ['O', ( 0., 0.    , 0.   )],
        ['H', ( 0., -0.757, 0.587)],
        ['H', ( 0., 0.757 , 0.587)],]
    mol.basis = {'H': 'cc-pvdz',
                 'O': 'cc-pvdz',}
    mol.build()

    m = scf.RHF(mol)
    ehf = m.scf()

    mc = mc1step.CASSCF(mol, m, 6, 4)
    mc.verbose = 4
    mo = m.mo_coeff.copy()

    eris0 = _ERIS(mc, mo, 'incore')
    eris1 = _ERIS(mc, mo, 'outcore')
    print('jc_pp', numpy.allclose(eris0.jc_pp, eris1.jc_pp))
    print('kc_pp', numpy.allclose(eris0.kc_pp, eris1.kc_pp))
    print('aapp ', numpy.allclose(eris0.aapp , eris1.aapp ))
    print('appa ', numpy.allclose(eris0.appa , eris1.appa ))
    print('Iapcv ', numpy.allclose(eris0.Iapcv , eris1.Iapcv ))
    print('Icvcv ', numpy.allclose(eris0.Icvcv , eris1.Icvcv ))

    ncore = mc.ncore
    ncas = mc.ncas
    nocc = ncore + ncas
    nmo = mo.shape[1]
    eri = ao2mo.incore.full(m._eri, mo, compact=False).reshape(nmo,nmo,nmo,nmo)
    aaap = numpy.array(eri[ncore:nocc,ncore:nocc,ncore:nocc,:])
    jc_pp = numpy.einsum('iipq->ipq', eri[:ncore,:ncore,:,:])
    kc_pp = numpy.einsum('ipqi->ipq', eri[:ncore,:,:,:ncore])
    aapp = numpy.array(eri[ncore:nocc,ncore:nocc,:,:])
    appa = numpy.array(eri[ncore:nocc,:,:,ncore:nocc])
    capv = eri[:ncore,ncore:nocc,:,ncore:]
    cvap = eri[:ncore,ncore:,ncore:nocc,:]
    cpav = eri[:ncore,:,ncore:nocc,ncore:]
    ccvv = eri[:ncore,:ncore,ncore:,ncore:]
    cvcv = eri[:ncore,ncore:,:ncore,ncore:]

    cVAp = cvap * 4 \
         - capv.transpose(0,3,1,2) \
         - cpav.transpose(0,3,2,1)
    cVCv = cvcv * 4 \
         - ccvv.transpose(0,3,1,2) \
         - cvcv.transpose(0,3,2,1)

    print('jc_pp', numpy.allclose(jc_pp, eris0.jc_pp))
    print('kc_pp', numpy.allclose(kc_pp, eris0.kc_pp))
    print('aapp ', numpy.allclose(aapp , eris0.aapp ))
    print('appa ', numpy.allclose(appa , eris0.appa ))
    print('Iapcv ', numpy.allclose(cVAp.transpose(2,3,0,1), eris1.Iapcv ))
    print('Icvcv ', numpy.allclose(cVCv.transpose(2,3,0,1), eris1.Icvcv ))