// ======================================================================
// \title  CfdpGuard.cpp
// \brief  The CFDP uplink's gatekeeper (see CfdpGuard.fpp)
//
// Both paths read the buffer the way CfdpManager::dataIn_handler does: a big-endian FW_PACKET_FILE descriptor,
// then the PDU, typed with peekPduType and decoded with F´'s own MetadataPdu / FinPdu. The guard therefore sees a
// Metadata exactly when cfdpManager would, and the destination it checks is the string cfdpManager would open.
// ======================================================================

#include "DoomMission/Components/CfdpGuard/CfdpGuard.hpp"

#include "DoomMission/Components/Doom/WadPath.hpp"
#include "Fw/Com/ComPacket.hpp"
#include "Fw/Types/SerialBuffer.hpp"

namespace DoomMission {

namespace {

using namespace Svc::Ccsds::Cfdp;

constexpr FwSizeType DESCRIPTOR_SIZE = sizeof(FwPacketDescriptorType);
static_assert(DESCRIPTOR_SIZE == 2, "the descriptor is rewritten as two bytes below");

//! The PDU behind the FW_PACKET_FILE descriptor, as CfdpManager::dataIn_handler cuts it; empty when cfdpManager
//! would not pass the buffer to its engine at all
Fw::Buffer pduView(Fw::Buffer& fwBuffer) {
    if (fwBuffer.getSize() < DESCRIPTOR_SIZE) {
        return Fw::Buffer();
    }
    FwPacketDescriptorType packetType = 0;
    const Fw::SerializeStatus status = fwBuffer.getDeserializer().deserializeTo(packetType);
    if (status != Fw::FW_SERIALIZE_OK || packetType != Fw::ComPacketType::FW_PACKET_FILE) {
        return Fw::Buffer();
    }
    return Fw::Buffer(fwBuffer.getData() + DESCRIPTOR_SIZE, fwBuffer.getSize() - DESCRIPTOR_SIZE,
                      fwBuffer.getContext());
}

//! A deserializer over exactly the view's bytes, as Engine::receivePdu builds it
Fw::SerialBuffer serialOver(const Fw::Buffer& pdu) {
    Fw::SerialBuffer sb(pdu.getData(), pdu.getSize());
    sb.setBuffLen(pdu.getSize());
    return sb;
}

}  // namespace

CfdpGuard ::CfdpGuard(const char* const compName)
    : CfdpGuardComponentBase(compName), m_refusedNext(0), m_order(0), m_accepted(0), m_refusedCount(0),
      m_committed(0) {
    for (FwSizeType i = 0; i < MAX_UPLOADS; i++) {
        this->m_uploads[i].used = false;
        this->m_refused[i].used = false;
    }
}

CfdpGuard ::~CfdpGuard() {}

// ----------------------------------------------------------------------
// Up: router -> cfdpManager
// ----------------------------------------------------------------------

void CfdpGuard ::uplinkIn_handler(FwIndexType portNum, Fw::Buffer& fwBuffer) {
    const Fw::Buffer pdu = pduView(fwBuffer);
    if (pdu.getSize() > 0 && peekPduType(pdu) == PduTypeEnum::METADATA && !this->admitMetadata(pdu)) {
        // cfdpManager hands back a buffer whose descriptor is not FW_PACKET_FILE without reading it, on its own
        // thread, through the dataInReturn path the router already has (CfdpManager::dataIn_handler).
        U8* const data = fwBuffer.getData();
        data[0] = 0;
        data[1] = static_cast<U8>(Fw::ComPacketType::FW_PACKET_UNKNOWN);
    }
    this->uplinkOut_out(0, fwBuffer);
}

bool CfdpGuard ::admitMetadata(const Fw::Buffer& pdu) {
    MetadataPdu md;
    Fw::SerialBuffer sb = serialOver(pdu);
    const Fw::SerializeStatus status = md.deserializeFrom(sb);
    if (status != Fw::FW_SERIALIZE_OK) {
        // cfdpManager could not read it either (FailMetadataPduDeserialization) and would open nothing
        this->log_WARNING_LO_MetadataUnreadable(static_cast<I32>(status));
        this->tlmWrite_UPLOADS_REFUSED(++this->m_refusedCount);
        return false;
    }
    const EntityId src = md.getSourceEid();
    const TransactionSeq seq = md.getTransactionSeq();
    const Fw::String& dest = md.getDestFilename();
    bool admit = WadPath::isUplinkPart(dest.toChar());

    bool resend = false;     // a Metadata already let through, sent again
    bool forgotten = false;  // an older upload had to make room
    Upload old;
    if (admit && md.getTxmMode() == Class::CLASS_2) {
        // Class 1 sends no FIN, so it is let through but not followed: only COMMIT_WAD, with its own checks, can put
        // a class 1 file in place
        Os::ScopeLock lock(this->m_lock);
        FwSizeType slot = MAX_UPLOADS;
        for (FwSizeType i = 0; i < MAX_UPLOADS && !resend; i++) {
            const Upload& u = this->m_uploads[i];
            if (u.used && u.srcEid == src && u.seq == seq) {
                // The same file, or nothing: cfdpManager keeps the first Metadata it saw, and a different
                // destination could only take effect if that one never did
                resend = true;
                admit = (u.dest == dest);
            } else if (!u.used && slot == MAX_UPLOADS) {
                slot = i;
            }
        }
        if (!resend) {
            if (slot == MAX_UPLOADS) {
                slot = 0;
                for (FwSizeType i = 1; i < MAX_UPLOADS; i++) {
                    if (this->m_uploads[i].order < this->m_uploads[slot].order) {
                        slot = i;
                    }
                }
                old = this->m_uploads[slot];
                forgotten = true;
            }
            Upload& u = this->m_uploads[slot];
            u.used = true;
            u.srcEid = src;
            u.seq = seq;
            u.destEid = md.getDestEid();
            u.order = this->m_order++;
            u.dest = dest;
        }
    }

    if (!admit) {
        bool told = false;
        {
            Os::ScopeLock lock(this->m_lock);
            for (FwSizeType i = 0; i < MAX_UPLOADS; i++) {
                const Refused& r = this->m_refused[i];
                told = told || (r.used && r.srcEid == src && r.seq == seq);
            }
            if (!told) {
                Refused& r = this->m_refused[this->m_refusedNext];
                this->m_refusedNext = (this->m_refusedNext + 1) % MAX_UPLOADS;
                r.used = true;
                r.srcEid = src;
                r.seq = seq;
            }
        }
        if (!told) {  // once per transaction: the sender resends a Metadata each time the receiver asks for it
            this->log_WARNING_HI_UploadRefused(dest, src, seq);
            this->tlmWrite_UPLOADS_REFUSED(++this->m_refusedCount);
        }
        return false;
    }
    if (resend) {
        return true;
    }
    if (forgotten) {
        this->log_WARNING_LO_UploadForgotten(old.dest, old.srcEid, old.seq);
    }
    this->tlmWrite_UPLOADS_ACCEPTED(++this->m_accepted);
    return true;
}

// ----------------------------------------------------------------------
// Down: cfdpManager -> com queue
// ----------------------------------------------------------------------

void CfdpGuard ::downlinkIn_handler(FwIndexType portNum, Fw::Buffer& fwBuffer) {
    const Fw::Buffer pdu = pduView(fwBuffer);
    // File data, most of what a downlink sends, is told apart by one bit and passes straight on
    if (pdu.getSize() > 0 && peekPduType(pdu) == PduTypeEnum::FINISHED) {
        this->settleFin(pdu);  // before the FIN leaves: the file is in place by the time the ground hears of it
    }
    this->downlinkOut_out(portNum, fwBuffer);
}

void CfdpGuard ::settleFin(const Fw::Buffer& pdu) {
    FinPdu fin;
    Fw::SerialBuffer sb = serialOver(pdu);
    // FinPdu starts out reading NO_ERROR / COMPLETE / RETAINED, so a FIN that did not decode must never count
    if (fin.deserializeFrom(sb) != Fw::FW_SERIALIZE_OK || fin.getDirection() != PduDirection::DIRECTION_TOWARD_SENDER) {
        return;
    }
    Fw::String dest;
    {
        Os::ScopeLock lock(this->m_lock);
        FwSizeType slot = MAX_UPLOADS;
        for (FwSizeType i = 0; i < MAX_UPLOADS; i++) {
            const Upload& u = this->m_uploads[i];
            if (u.used && u.srcEid == fin.getSourceEid() && u.seq == fin.getTransactionSeq() &&
                u.destEid == fin.getDestEid()) {
                slot = i;
            }
        }
        if (slot == MAX_UPLOADS) {
            return;  // not an upload the guard let through, or one already settled: a repeated FIN commits nothing
        }
        dest = this->m_uploads[slot].dest;
        this->m_uploads[slot].used = false;
    }
    // The receiver sends NO_ERROR with RETAINED only after its checksum over the whole file matched, with the file
    // at the Metadata's destination (TransactionRx r2CalcCrcChunk); any other FIN leaves the file as it is
    const ConditionCode cc = fin.getConditionCode();
    const FinFileStatus fs = fin.getFileStatus();
    if (cc == ConditionCode::CONDITION_CODE_NO_ERROR && fs == FinFileStatus::FIN_FILE_STATUS_RETAINED &&
        fin.getDeliveryCode() == FinDeliveryCode::FIN_DELIVERY_CODE_COMPLETE) {
        this->log_ACTIVITY_HI_UploadCommitted(dest, fin.getSourceEid(), fin.getTransactionSeq());
        this->tlmWrite_UPLOADS_COMMITTED(++this->m_committed);
        if (this->isConnected_fileAnnounceOut_OutputPort(0)) {
            this->fileAnnounceOut_out(0, dest);
        }
    } else {
        this->log_WARNING_LO_UploadNotCommitted(dest, fin.getSourceEid(), fin.getTransactionSeq(),
                                                static_cast<U8>(cc), static_cast<U8>(fs));
    }
}

}  // namespace DoomMission
